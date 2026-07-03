"""Google Earth Engine and Drive access used by the workflow."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import time
from datetime import datetime
from typing import Any, Callable

import ee
import httplib2
import rasterio
from google.auth.credentials import Credentials

from .raster_naming import preferred_raster_product_stem, raster_product_stem, raster_semantic_key

LOGGER = logging.getLogger(__name__)


class _PerFileDownloadFilter(logging.Filter):
    """Hide noisy per-file transfer progress from normal INFO logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if record.levelno == logging.INFO and (
            message.startswith("Downloading ")
            or message.startswith("Downloaded ")
            or message.startswith("Skipping already-downloaded export:")
        ):
            return False
        return True


LOGGER.addFilter(_PerFileDownloadFilter())

ESSENTIAL_CSV_PREFIXES = (
    "Area_30m_",
    "ChangeArea_",
    "ForestChange_",
    "ChangeAgreement_",
    "MapBiomas_",
    "Temporal",
    "Municipal",
)

ESSENTIAL_CSV_NAMES = {
    "change_area_by_interval.csv",
    "change_area_forest_to_nonforest.csv",
    "forest_change_area_timeseries.csv",
    "temporal_consistency_reversals.csv",
    "agreement_metrics.csv",
    "mapbiomas_reclassification_schema.csv",
}


def _is_essential_csv_export(name: str) -> bool:
    """Return True for CSV exports used by final figures, report, or key Sheets tables."""
    clean_name = str(name)
    return clean_name in ESSENTIAL_CSV_NAMES or clean_name.startswith(ESSENTIAL_CSV_PREFIXES)


def initialize_earth_engine(project: str, credentials: Credentials | None = None) -> None:
    """Initialize Earth Engine with the configured Google Cloud project."""
    _configure_google_project(project)
    try:
        ee.Initialize(
            project=project,
            credentials=credentials,
            opt_url="https://earthengine-highvolume.googleapis.com",
            http_transport=httplib2.Http(timeout=30),
        )
    except Exception as exc:
        raise RuntimeError(
            "Earth Engine could not be initialized. Run `earthengine authenticate` "
            "or configure Google OAuth credentials."
        ) from exc


def _configure_google_project(project: str | None) -> None:
    """Set Google project environment variables used by Google auth clients."""
    if not project:
        return
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project)
    os.environ.setdefault("GCLOUD_PROJECT", project)
    os.environ.setdefault("GOOGLE_CLOUD_QUOTA_PROJECT", project)


def load_google_credentials(settings: dict[str, Any]) -> Credentials:
    """Load Google credentials for Drive downloads and Earth Engine execution."""
    import google.auth
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials as OAuthCredentials

    google_cfg = settings["google"]
    scopes = list(google_cfg["scopes"])
    project = settings["earth_engine"]["project"]
    _configure_google_project(project)
    token_path = Path(google_cfg["credentials_token"])
    client_path = Path(google_cfg["oauth_client_file"])
    gcloud_login_command = _gcloud_adc_login_command(scopes, project)
    client_login_command = _gcloud_client_login_command(scopes, project, client_path)
    _ignore_missing_explicit_adc()

    credentials: Credentials | None = None
    request = Request()
    if token_path.exists():
        credentials = OAuthCredentials.from_authorized_user_file(str(token_path), scopes)
        if not _credentials_include_scopes(credentials, scopes):
            LOGGER.info("Saved Google token is missing required scopes; reauthorization is required.")
            _archive_invalid_token(token_path)
            credentials = None

    if credentials and not credentials.valid and credentials.refresh_token:
        try:
            credentials.refresh(request)
            token_path.write_text(credentials.to_json(), encoding="utf-8")
        except Exception:
            LOGGER.info("Saved Google token could not be refreshed; reauthorization is required.")
            _archive_invalid_token(token_path)
            credentials = None

    if not credentials or not credentials.valid:
        if client_path.exists():
            try:
                from google_auth_oauthlib.flow import InstalledAppFlow
            except ModuleNotFoundError as exc:
                raise RuntimeError(
                    "The OAuth client file exists, but google-auth-oauthlib is not installed. "
                    "Install dependencies with `pip install -r requirements.txt`."
                ) from exc
            flow = InstalledAppFlow.from_client_secrets_file(str(client_path), scopes)
            credentials = flow.run_local_server(port=0)
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(credentials.to_json(), encoding="utf-8")
        else:
            try:
                credentials, _ = google.auth.default(scopes=scopes)
            except Exception as exc:
                raise RuntimeError(
                    "Google credentials were not found. To open the login window, first save a "
                    f"Desktop OAuth client JSON as `{client_path}`, then run "
                    "`python main.py --auth-only`."
                ) from exc
            if not credentials.valid and credentials.refresh_token:
                try:
                    credentials.refresh(request)
                except Exception as exc:
                    raise RuntimeError(
                        "Application Default Credentials could not be refreshed. "
                        "The existing ADC token is likely expired or revoked. Run "
                        "`gcloud auth application-default revoke --quiet`, then run "
                        f"`{gcloud_login_command}`. If Google shows 'This app is blocked', "
                        f"create a Desktop OAuth client, save it as `{client_path}`, and run "
                        f"`{client_login_command}` or `python main.py --auth-only`."
                    ) from exc

    if not credentials or not credentials.valid:
        raise RuntimeError(
            "Google credentials are unavailable or invalid. Run "
            f"`{gcloud_login_command}`. If Google blocks that app for Drive/Docs scopes, "
            f"create a Desktop OAuth client, save it as `{client_path}`, and run "
            f"`{client_login_command}` or `python main.py --auth-only`."
        )
    if hasattr(credentials, "with_quota_project"):
        credentials = credentials.with_quota_project(project)
    return credentials


def _credentials_include_scopes(credentials: Credentials, scopes: list[str]) -> bool:
    granted = set(getattr(credentials, "scopes", None) or [])
    if not granted:
        granted = set(getattr(credentials, "granted_scopes", None) or [])
    return not granted or set(scopes).issubset(granted)


def _gcloud_adc_login_command(scopes: list[str], project: str) -> str:
    return (
        "gcloud auth application-default login "
        f'--scopes "{",".join(scopes)}" '
        f"--project {project}"
    )


def _gcloud_client_login_command(scopes: list[str], project: str, client_path: Path) -> str:
    return (
        "gcloud auth application-default login "
        f"--client-id-file {client_path} "
        f'--scopes "{",".join(scopes)}" '
        f"--project {project}"
    )


def _ignore_missing_explicit_adc() -> None:
    explicit_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if explicit_path and not Path(explicit_path).exists():
        LOGGER.warning(
            "Ignoring GOOGLE_APPLICATION_CREDENTIALS because the file does not exist: %s",
            explicit_path,
        )
        os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)


def _archive_invalid_token(token_path: Path) -> None:
    """Move an unusable OAuth token aside so the next run does not retry it."""
    if not token_path.exists():
        return
    archive_path = token_path.with_suffix(token_path.suffix + ".invalid")
    try:
        token_path.replace(archive_path)
    except OSError:
        token_path.unlink(missing_ok=True)


def build_drive_service(credentials: Credentials) -> Any:
    """Build a Google Drive API client."""
    import google_auth_httplib2
    from googleapiclient.discovery import build

    http = google_auth_httplib2.AuthorizedHttp(credentials, http=httplib2.Http(timeout=120))
    return build("drive", "v3", http=http, cache_discovery=False)


def build_docs_service(credentials: Credentials) -> Any:
    """Build a Google Docs API client."""
    from googleapiclient.discovery import build

    return build("docs", "v1", credentials=credentials, cache_discovery=False)


def build_sheets_service(credentials: Credentials) -> Any:
    """Build a Google Sheets API client."""
    from googleapiclient.discovery import build

    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def download_drive_exports(drive_service: Any, settings: dict[str, Any], target_directory: Path) -> list[Path]:
    """Download Earth Engine CSV exports from the configured Drive folder."""
    target_directory.mkdir(parents=True, exist_ok=True)
    root_id = settings["google"].get("drive_folder_id")
    if not root_id:
        root_id = _find_drive_folder(drive_service, settings["google"]["drive_folder_name"])
    export_folder = settings["google"]["drive_subfolders"]["exports"]
    export_id = _find_drive_folder(drive_service, export_folder, parent_id=root_id)

    downloaded: list[Path] = []
    for folder_id in (root_id, export_id):
        page_token: str | None = None
        while True:
            list_kwargs: dict[str, Any] = {
                "q": f"'{folder_id}' in parents and trashed=false",
                "fields": "nextPageToken,files(id,name,mimeType,size,modifiedTime)",
                "pageSize": 1000,
            }
            if page_token:
                list_kwargs["pageToken"] = page_token
            response = drive_service.files().list(**list_kwargs).execute()
            for item in response.get("files", []):
                name = str(item["name"])
                if not name.lower().endswith(".csv"):
                    continue
                if not name.startswith((
                    "Area_30m_",
                    "XTab_30m_",
                    "FCBM_XTab_",
                    "DerivedBinary_XTab_",
                    "RiskMap_XTab_",
                    "AllClass_XTab_",
                    "Binary_XTab_",
                    "ChangeAgreement_",
                    "ChangeArea_",
                    "ChangeAreaTimeSeries_",
                    "SpatialDisagreement_",
                    "TemporalReversal_",
                    "MunicipalArea_",
                )):
                    continue
                target = target_directory / name
                _download_file(drive_service, item["id"], target, expected_size=_drive_file_size(item))
                downloaded.append(target)
                LOGGER.debug("Downloaded %s", target.name)
            page_token = response.get("nextPageToken")
            if not page_token:
                break
    return sorted(set(downloaded))


def download_drive_raster_exports(
    drive_service: Any,
    settings: dict[str, Any],
    target_directory: Path,
    progress_callback: Callable[[str, str], None] | None = None,
) -> list[Path]:
    """Download completed GeoTIFF raster exports from Google Drive."""
    target_directory.mkdir(parents=True, exist_ok=True)
    drive_items = list_drive_raster_exports(drive_service, settings)

    downloaded: list[Path] = []
    preferred_stems = _preferred_drive_raster_stems(drive_items, settings, target_directory)
    for item in drive_items:
        name = str(item["name"])
        target = target_directory / name
        if not _raster_export_name_matches_target_grid(name, settings):
            if target.exists():
                _unlink_download_with_manifests(target)
                LOGGER.warning("Removed local legacy raster export: %s", name)
            LOGGER.info("Skipping redundant raster export outside target grid naming: %s", name)
            continue
        product_stem = raster_product_stem(name)
        semantic_key = raster_semantic_key(product_stem)
        preferred_stem = preferred_stems.get(semantic_key, product_stem)
        if product_stem != preferred_stem:
            if target.exists():
                _unlink_download_with_manifests(target)
                LOGGER.warning("Removed local duplicate raster export: %s", name)
            LOGGER.info("Skipping duplicate raster export %s; preferred product is %s.", name, preferred_stem)
            continue
        if _rejected_download_matches(target, item):
            LOGGER.info("Skipping previously rejected non-target raster export: %s", name)
            continue
        if target.exists() and not _local_geotiff_matches_target_grid(target):
            target.unlink(missing_ok=True)
            _download_manifest_path(target).unlink(missing_ok=True)
            _write_rejected_download_manifest(target, item, "local GeoTIFF is not EPSG:5880 at 30 m")
            LOGGER.warning("Removed local non-target raster export: %s", name)
            continue
        if _drive_export_is_empty(item):
            target.unlink(missing_ok=True)
            _download_manifest_path(target).unlink(missing_ok=True)
            LOGGER.warning(
                "Skipping empty Drive export %s. The Earth Engine export must be generated again.",
                item.get("name", target.name),
            )
            continue
        if _local_download_is_current(target, item):
            LOGGER.debug("Skipping already-downloaded export: %s", target.name)
            continue
        expected_size = _drive_file_size(item)
        size_note = f" ({expected_size / (1024 * 1024):.1f} MiB)" if expected_size is not None else ""
        LOGGER.info("Raster download starting: %s%s", target.name, size_note)
        if progress_callback is not None:
            progress_callback("Downloading", f"{target.name}{size_note}")
        _download_file(
            drive_service,
            item["id"],
            target,
            expected_size=expected_size,
            progress_label="Raster download",
            progress_callback=progress_callback,
        )
        if progress_callback is not None:
            progress_callback("Compressing", target.name)
        if not _local_geotiff_matches_target_grid(target):
            _write_rejected_download_manifest(target, item, "downloaded GeoTIFF is not EPSG:5880 at 30 m")
            target.unlink(missing_ok=True)
            _download_manifest_path(target).unlink(missing_ok=True)
            LOGGER.warning("Rejected non-target raster export after download: %s", name)
            if progress_callback is not None:
                progress_callback("Rejected", target.name)
            continue
        _rewrite_geotiff_lzw(target)
        _write_download_manifest(target, item)
        downloaded.append(target)
        LOGGER.info("Raster download finished: %s", target.name)
        if progress_callback is not None:
            progress_callback("Downloaded", target.name)
    return sorted(set(downloaded))


def list_drive_raster_exports(drive_service: Any, settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Return GeoTIFF raster export metadata from Drive without downloading files."""
    root_id = settings["google"].get("drive_folder_id")
    if not root_id:
        root_id = _find_drive_folder(drive_service, settings["google"]["drive_folder_name"])
    raster_folder = settings["google"]["drive_subfolders"].get("rasters", "02_raster_exports")
    raster_id = _ensure_drive_folder(drive_service, raster_folder, parent_id=root_id)

    drive_items: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        list_kwargs: dict[str, Any] = {
            "q": f"'{raster_id}' in parents and trashed=false",
            "fields": "nextPageToken,files(id,name,mimeType,size,modifiedTime)",
            "pageSize": 1000,
        }
        if page_token:
            list_kwargs["pageToken"] = page_token
        response = drive_service.files().list(**list_kwargs).execute()
        for item in response.get("files", []):
            name = str(item["name"])
            if not name.lower().endswith((".tif", ".tiff")):
                continue
            drive_items.append(item)
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return drive_items

def drive_raster_semantic_keys(drive_service: Any, settings: dict[str, Any]) -> set[str]:
    """Return semantic keys for target-grid GeoTIFF raster exports already present in Drive."""
    return {
        raster_semantic_key(raster_product_stem(str(item["name"])))
        for item in list_drive_raster_exports(drive_service, settings)
        if _raster_export_name_matches_target_grid(str(item["name"]), settings)
        and not _drive_export_is_empty(item)
    }


def _target_raster_suffix(settings: dict[str, Any]) -> str:
    grid = settings.get("grid", {})
    earth_engine = settings.get("earth_engine", {})
    crs = str(grid.get("crs") or earth_engine.get("crs") or "EPSG:5880")
    scale = grid.get("scale_m") or earth_engine.get("scale_native_m") or 30
    return f"{crs.replace(':', '_').replace('/', '_')}_{scale}m"


def _raster_export_name_matches_target_grid(name: str, settings: dict[str, Any]) -> bool:
    product_stem = raster_product_stem(name)
    return product_stem.endswith(_target_raster_suffix(settings))


def _preferred_drive_raster_stems(
    drive_items: list[dict[str, Any]],
    settings: dict[str, Any],
    target_directory: Path,
) -> dict[str, str]:
    candidates_by_key: dict[str, set[str]] = {}
    for item in drive_items:
        name = str(item["name"])
        if _raster_export_name_matches_target_grid(name, settings):
            product_stem = raster_product_stem(name)
            candidates_by_key.setdefault(raster_semantic_key(product_stem), set()).add(product_stem)

    if target_directory.exists():
        for path in target_directory.iterdir():
            if path.is_file() and path.suffix.lower() in {".tif", ".tiff"}:
                product_stem = raster_product_stem(path)
                candidates_by_key.setdefault(raster_semantic_key(product_stem), set()).add(product_stem)

    return {
        key: preferred_raster_product_stem(stems)
        for key, stems in candidates_by_key.items()
        if stems
    }


def _unlink_download_with_manifests(path: Path) -> None:
    path.unlink(missing_ok=True)
    _download_manifest_path(path).unlink(missing_ok=True)
    _rejected_download_manifest_path(path).unlink(missing_ok=True)


def upload_report_image(drive_service: Any, settings: dict[str, Any], image_path: Path) -> str:
    """Upload or replace a report image and return a public image URL."""
    from googleapiclient.http import MediaFileUpload

    root_id = settings["google"].get("drive_folder_id")
    if not root_id:
        root_id = _ensure_drive_folder(drive_service, settings["google"]["drive_folder_name"])
    asset_folder = _ensure_drive_folder(
        drive_service,
        settings["google"]["drive_subfolders"].get("report_assets", "report_assets"),
        parent_id=root_id,
    )
    existing = _find_drive_file(drive_service, image_path.name, parent_id=asset_folder)
    mime_type = "image/jpeg" if image_path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
    media = MediaFileUpload(str(image_path), mimetype=mime_type, resumable=False)
    if existing:
        file_id = str(existing["id"])
        drive_service.files().update(fileId=file_id, media_body=media, fields="id").execute()
    else:
        created = drive_service.files().create(
            body={"name": image_path.name, "parents": [asset_folder]},
            media_body=media,
            fields="id",
        ).execute()
        file_id = str(created["id"])
    _make_file_readable(drive_service, file_id)
    return f"https://drive.google.com/uc?id={file_id}"


def publish_powerpoint_as_google_slides(
    drive_service: Any,
    settings_path: Path,
    settings: dict[str, Any],
    presentation_path: Path,
) -> str:
    """Upload the local PowerPoint file as a Google Slides presentation."""
    from googleapiclient.http import MediaFileUpload

    _ = settings_path
    root_id = settings["google"].get("drive_folder_id")
    if not root_id:
        root_id = _ensure_drive_folder(drive_service, settings["google"]["drive_folder_name"])
    presentation_id = str(settings["google"].get("presentation_id") or "").strip()
    if not presentation_id:
        raise RuntimeError("google.presentation_id must be configured before publishing Google Slides.")
    media = MediaFileUpload(
        str(presentation_path),
        mimetype="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        resumable=False,
    )
    drive_service.files().update(fileId=presentation_id, media_body=media, fields="id").execute()
    return presentation_id


def ensure_drive_raster_folder(drive_service: Any, settings: dict[str, Any]) -> str:
    """Ensure the raster export subfolder exists inside the project Drive folder.

    Earth Engine resolves the export folder by name only and will create it at
    the My Drive root if no matching folder exists. This function pre-creates
    the folder in the correct location and moves it there if EE already placed
    it at the Drive root.
    """
    root_id = settings["google"].get("drive_folder_id")
    if not root_id:
        root_id = _ensure_drive_folder(drive_service, settings["google"]["drive_folder_name"])
    raster_folder = settings["google"]["drive_subfolders"].get("rasters", "02_raster_exports")

    existing = _find_drive_file(drive_service, raster_folder, parent_id=root_id)
    if existing:
        return str(existing["id"])

    misplaced = _find_drive_file(drive_service, raster_folder)
    if misplaced:
        folder_id = str(misplaced["id"])
        file_meta = drive_service.files().get(fileId=folder_id, fields="parents").execute()
        current_parents = ",".join(file_meta.get("parents", []))
        drive_service.files().update(
            fileId=folder_id,
            addParents=root_id,
            removeParents=current_parents,
            fields="id,parents",
        ).execute()
        LOGGER.info("Moved raster export folder %s into project folder (id=%s).", raster_folder, folder_id)
        return folder_id

    folder_id = _ensure_drive_folder(drive_service, raster_folder, parent_id=root_id)
    LOGGER.info("Created raster export folder %s (id=%s).", raster_folder, folder_id)
    return folder_id


def _find_drive_file(drive_service: Any, name: str, parent_id: str | None = None) -> dict[str, Any] | None:
    terms = [f"name='{_drive_literal(name)}'", "trashed=false"]
    if parent_id:
        terms.append(f"'{parent_id}' in parents")
    response = drive_service.files().list(
        q=" and ".join(terms),
        fields="files(id,name,mimeType)",
    ).execute()
    files = response.get("files", [])
    return files[0] if files else None


def _ensure_drive_folder(drive_service: Any, name: str, parent_id: str | None = None) -> str:
    found = _find_drive_file(drive_service, name, parent_id=parent_id)
    if found:
        return str(found["id"])
    metadata: dict[str, Any] = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    if parent_id:
        metadata["parents"] = [parent_id]
    created = drive_service.files().create(body=metadata, fields="id").execute()
    return str(created["id"])


def _find_drive_folder(drive_service: Any, name: str, parent_id: str | None = None) -> str:
    terms = [
        f"name='{_drive_literal(name)}'",
        "mimeType='application/vnd.google-apps.folder'",
        "trashed=false",
    ]
    if parent_id:
        terms.append(f"'{parent_id}' in parents")
    response = drive_service.files().list(q=" and ".join(terms), fields="files(id,name)").execute()
    files = response.get("files", [])
    if not files:
        raise FileNotFoundError(f"Google Drive folder not found: {name}")
    return str(files[0]["id"])


def _drive_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _make_file_readable(drive_service: Any, file_id: str) -> None:
    try:
        drive_service.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
            fields="id",
        ).execute()
    except Exception:
        LOGGER.warning("Could not set public read permission for image %s.", file_id)


def _local_download_is_current(target: Path, drive_item: dict[str, Any]) -> bool:
    """Return True when a local export already matches the Drive file metadata."""
    if not target.exists() or not target.is_file():
        return False
    local_size = target.stat().st_size
    if local_size <= 0:
        target.unlink(missing_ok=True)
        _download_manifest_path(target).unlink(missing_ok=True)
        return False
    if _download_manifest_matches(target, drive_item):
        return True
    remote_size = _drive_file_size(drive_item)
    if remote_size is None:
        return True
    if remote_size <= 0:
        target.unlink(missing_ok=True)
        _download_manifest_path(target).unlink(missing_ok=True)
        return False
    drive_modified_at = _drive_modified_timestamp(drive_item)
    if (
        target.suffix.lower() in {".tif", ".tiff"}
        and _local_geotiff_is_readable(target)
        and drive_modified_at is not None
        and target.stat().st_mtime >= drive_modified_at
    ):
        _write_download_manifest(target, drive_item, assumed=True)
        LOGGER.info("Backfilled Drive manifest for existing raster %s.", target.name)
        return True
    return local_size == remote_size


def _download_manifest_path(target: Path) -> Path:
    return target.with_suffix(target.suffix + ".drive.json")


def _rejected_download_manifest_path(target: Path) -> Path:
    return target.with_suffix(target.suffix + ".rejected.json")


def _download_manifest_matches(target: Path, drive_item: dict[str, Any]) -> bool:
    manifest_path = _download_manifest_path(target)
    if not manifest_path.exists():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if str(manifest.get("id", "")) != str(drive_item.get("id", "")):
        return False
    expected_size = _drive_file_size(drive_item)
    if expected_size is not None and manifest.get("size") != expected_size:
        return False
    return True


def _rejected_download_matches(target: Path, drive_item: dict[str, Any]) -> bool:
    manifest_path = _rejected_download_manifest_path(target)
    if not manifest_path.exists():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if str(manifest.get("id", "")) != str(drive_item.get("id", "")):
        return False
    expected_size = _drive_file_size(drive_item)
    if expected_size is not None and manifest.get("size") != expected_size:
        return False
    return True


def _write_download_manifest(target: Path, drive_item: dict[str, Any], *, assumed: bool = False) -> None:
    manifest = {
        "id": str(drive_item.get("id", "")),
        "name": str(drive_item.get("name", target.name)),
        "size": _drive_file_size(drive_item),
        "modifiedTime": drive_item.get("modifiedTime"),
        "assumed_from_existing_local_file": assumed,
    }
    _download_manifest_path(target).write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _write_rejected_download_manifest(target: Path, drive_item: dict[str, Any], reason: str) -> None:
    manifest = {
        "id": str(drive_item.get("id", "")),
        "name": str(drive_item.get("name", target.name)),
        "size": _drive_file_size(drive_item),
        "modifiedTime": drive_item.get("modifiedTime"),
        "rejected_reason": reason,
    }
    _rejected_download_manifest_path(target).write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _local_geotiff_is_readable(target: Path) -> bool:
    try:
        with rasterio.open(target) as dataset:
            return dataset.count >= 1 and dataset.width > 0 and dataset.height > 0
    except Exception:
        return False


def _local_geotiff_matches_target_grid(target: Path) -> bool:
    try:
        with rasterio.open(target) as dataset:
            return (
                str(dataset.crs) == "EPSG:5880"
                and abs(abs(dataset.transform.a) - 30.0) <= 1e-6
                and abs(abs(dataset.transform.e) - 30.0) <= 1e-6
            )
    except Exception:
        return False


def _drive_file_size(drive_item: dict[str, Any]) -> int | None:
    """Return Drive's reported binary size when it is available and valid."""
    drive_size = drive_item.get("size")
    if drive_size is None:
        return None
    try:
        return int(drive_size)
    except (TypeError, ValueError):
        return None


def _drive_modified_timestamp(drive_item: dict[str, Any]) -> float | None:
    modified_time = drive_item.get("modifiedTime")
    if not modified_time:
        return None
    try:
        return datetime.fromisoformat(str(modified_time).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _drive_export_is_empty(drive_item: dict[str, Any]) -> bool:
    """Return True when Drive reports an empty exported CSV, which indicates a failed EE export."""
    if "size" not in drive_item:
        return False
    try:
        return int(drive_item["size"]) <= 0
    except (TypeError, ValueError):
        return False


def _download_file(
    drive_service: Any,
    file_id: str,
    target: Path,
    *,
    expected_size: int | None = None,
    progress_label: str | None = None,
    progress_callback: Callable[[str, str], None] | None = None,
    attempts: int = 6,
) -> None:
    from googleapiclient.http import MediaIoBaseDownload

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".download")
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        temporary.unlink(missing_ok=True)
        try:
            request = drive_service.files().get_media(fileId=file_id)
            with temporary.open("wb") as output:
                downloader = MediaIoBaseDownload(output, request, chunksize=1024 * 1024)
                done = False
                last_percent = -1
                while not done:
                    status, done = downloader.next_chunk(num_retries=3)
                    if progress_label and status is not None:
                        percent = min(100, int(status.progress() * 100))
                        if percent >= last_percent + 10 or done:
                            LOGGER.info("%s %s: %d%%", progress_label, target.name, percent)
                            if progress_callback is not None:
                                progress_callback("Downloading", f"{target.name} ({percent}%)")
                            last_percent = percent

            downloaded_size = temporary.stat().st_size if temporary.exists() else 0
            if downloaded_size <= 0:
                raise RuntimeError(f"Downloaded empty file: {target.name}")
            if expected_size is not None and downloaded_size != expected_size:
                raise RuntimeError(
                    f"Downloaded size mismatch for {target.name}: "
                    f"got {downloaded_size} bytes, expected {expected_size} bytes"
                )
            temporary.replace(target)
            return
        except KeyboardInterrupt:
            temporary.unlink(missing_ok=True)
            LOGGER.warning("Interrupted while downloading %s; removed partial download.", target.name)
            raise
        except Exception as exc:
            last_error = exc
            temporary.unlink(missing_ok=True)
            if attempt >= attempts:
                break
            delay_seconds = min(60, 2 ** (attempt - 1))
            LOGGER.warning(
                "Download attempt %d/%d failed for %s: %s. Retrying in %d s.",
                attempt,
                attempts,
                target.name,
                exc,
                delay_seconds,
            )
            time.sleep(delay_seconds)
    raise RuntimeError(f"Could not download {target.name} after {attempts} attempts.") from last_error


def _rewrite_geotiff_lzw(path: Path) -> None:
    """Rewrite a downloaded GeoTIFF with LZW compression."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    with rasterio.open(path) as source:
        profile = source.profile.copy()
        profile.update(compress="lzw")
        with rasterio.open(temporary, "w", **profile) as target:
            for band_index in range(1, source.count + 1):
                target.write(source.read(band_index), band_index)
            target.update_tags(**source.tags())
            for band_index in range(1, source.count + 1):
                target.update_tags(band_index, **source.tags(band_index))
    temporary.replace(path)


def export_google_doc_as_word(drive_service: Any, document_id: str, target: Path) -> Path:
    """Export a Google Docs document to Word, failing clearly if Drive returns no file."""
    from googleapiclient.http import MediaIoBaseDownload

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".download")
    temporary.unlink(missing_ok=True)

    request = drive_service.files().export_media(
        fileId=document_id,
        mimeType="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    with temporary.open("wb") as output:
        downloader = MediaIoBaseDownload(output, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

    if not temporary.exists() or temporary.stat().st_size <= 0:
        temporary.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        raise RuntimeError(
            f"Google Docs export did not create a valid Word file: {target}. "
            "Retry the report export; this is usually a transient Google Drive export failure."
        )

    temporary.replace(target)
    return target

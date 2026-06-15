# Notice

This repository is an adapted version of the original UDef-ARP project:

https://github.com/ClarkCGA/UDef-ARP

## Upstream Attribution

UDef-ARP, the Unplanned Deforestation Allocated Risk Modeling and Mapping
Procedure, was developed by Clark Labs at Clark University in collaboration with
TerraCarbon to support implementation of Verra's VT0007 Unplanned Deforestation
Allocation tool.

Original upstream repository:

```text
ClarkCGA/UDef-ARP
```

Upstream reference checked for this fork:

```text
tag:    v2.14.1
commit: 14acdaf0738a81e93bc8abf41a7fcd8abbbf0a7a
```

Protocol reference:

```text
Verra VT0007 Unplanned Deforestation Allocation v1.0
https://verra.org/wp-content/uploads/2024/02/VT0007-Unplanned-Deforestation-Allocation-v1.0.pdf
```

## Adaptation Notes

This repository is not presented as a direct unmodified copy of the Clark Labs
repository. It is an adapted version that preserves the original GPLv3 license
and upstream project attribution. Unless otherwise stated, the application code,
GUI files, documentation PDFs, images, logos, and fonts originate from the
upstream UDef-ARP project.

Adaptation-specific changes include YAML-driven workflow automation, exact
input-layer conventions, repository documentation polish, GitHub Actions syntax
checks, `.gitignore`, and a documented fix that stores modeling region IDs as
`numpy.int32` / `GDT_Int32` to avoid `int16` overflow in jurisdictions with many
administrative divisions.

## License

The project is distributed under the GNU General Public License v3. See
[`LICENSE`](LICENSE).

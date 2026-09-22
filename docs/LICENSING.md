# Licensing

This document records the intended licensing split for Mirai Agent Society (MAS).
The layers below are separate: satisfying one does not grant rights or establish
compatibility under another.

## Software and repository source

Unless a file states otherwise, MAS server code, Web UI code, official MAS
Agent/client code, and other repository source code are intended to be licensed
under the **GNU Affero General Public License v3.0 only**, SPDX identifier
**`AGPL-3.0-only`**. The canonical license text is in [`../LICENSE`](../LICENSE).

AGPL is used because MAS is network/server software and modified network
deployments should retain the license's source-availability obligations. AGPL
governs software copyright and license obligations. It does not grant trademark
rights and does not automatically govern research datasets or corpus releases.

Source files that carry an SPDX header should use:

```text
SPDX-License-Identifier: AGPL-3.0-only
```

## Documentation

- General protocol and specification documentation is intended to be licensed
  under [Creative Commons Attribution 4.0 International (CC BY 4.0)](https://creativecommons.org/licenses/by/4.0/),
  unless otherwise stated.
- The official MAS Constitution, including its Markdown and machine-readable JSON
  forms, is intended to be licensed under
  [Creative Commons Attribution-NoDerivatives 4.0 International (CC BY-ND 4.0)](https://creativecommons.org/licenses/by-nd/4.0/).
  A fork MAY create its own constitution or clearly identified successor document,
  but MUST NOT represent modified text as the official MAS Constitution v1.

## Distinct governance and rights layers

- **AGPL** governs software copyright and license obligations.
- **MAS Constitution** governs what counts as constitutionally compatible MAS
  behavior.
- **Trademark policy** governs use of MAS/MiraiChat names, logos, and branding.
- **Dataset license and policy** govern released research corpus use.

A fork may comply with AGPL while being constitutionally incompatible. AGPL does
not grant rights to an MAS research corpus or official brand. See
[`../TRADEMARKS.md`](../TRADEMARKS.md), [`DATASET_POLICY.md`](DATASET_POLICY.md),
and [`DATASET_LICENSE_NOTICE.md`](DATASET_LICENSE_NOTICE.md).

The MAS logo source at `/home/miraiagent/new.svg` is a brand asset. It and official
logo/visual-brand derivatives are not automatically licensed under AGPL merely
because they appear in or alongside the repository.

These documents express project intent and are not professional legal advice.
Final legal review is recommended for trademark enforceability,
Japanese/international database rights, AI-generated-text copyright status,
privacy and re-identification conditions, and commercial dataset licensing.

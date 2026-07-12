This prerelease contains research developer component packages built from one exact tagged commit.

Included assets:

- committed source archive;
- API and worker wheels/source distributions;
- static web distribution archive;
- deterministic build metadata, release notice, and SHA-256 checksums.

This is not the complete signed air-gap/Bazzite deployment bundle. It excludes OCI images,
models, cache assets, DICOM, site data, secrets, and harmonization profiles. Read
`DEVELOPER_RELEASE_NOTICE.txt` before using the assets.

Verify the downloaded directory locally with:

```bash
sha256sum --check SHA256SUMS
```

Verify GitHub build provenance for an individual asset with:

```bash
gh attestation verify <asset> --repo <owner/repository>
```

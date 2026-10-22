# Third-party components

## XHS-Downloader

Source: https://github.com/JoeanAmier/XHS-Downloader

License: GNU General Public License v3.0. Source and license are available from the upstream repository and the pinned [2.7 release](https://github.com/JoeanAmier/XHS-Downloader/releases/tag/2.7).

This repository connects to its HTTP API. The upstream binary and runtime are not committed here. The optional installer downloads the official Windows x64 2.7 archive and verifies SHA256 `ff5e7b6355895d5d18232f5db69d5b08c7237720c2c8c57b1d9b5bde8fa40c99`.

## Swagger UI

Version: 5.17.14. Source: https://github.com/swagger-api/swagger-ui

The local documentation assets come from the `swagger-ui-dist` npm package. The npm registry SHA512 integrity hash was verified during vendoring. The upstream LICENSE and NOTICE are preserved in `前端/vendor/`.

## Playwright

Author collection uses Microsoft Playwright, installed separately through npm; its source and license are available at https://github.com/microsoft/playwright. No browser binaries or user profiles are included in this repository.

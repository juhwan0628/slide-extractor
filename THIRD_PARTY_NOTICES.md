# Third-party software notices

Slide Extractor application code is copyright (c) 2026 Juhwan Heo and is distributed under the MIT License (`LICENSE`). These terms do not replace the licenses of the components below. Original license/copyright texts are provided in the installed `licenses/` directory.

## Runtime components

| Component | Pinned version | License / materials |
|---|---|---|
| Python | 3.12 (patch recorded in build-manifest.json) | Python Software Foundation; standard-library notices in licenses/Python-LICENSE.txt |
| NumPy | 2.5.3 | BSD-3-Clause and vendored notices, including applicable numerical runtime notices |
| OpenCV / opencv-python-headless | 5.0.0 / 5.0.0.93 | OpenCV Apache-2.0; packaging MIT; source-built core/imgproc/imgcodecs/python3 only |
| Pillow | 12.3.0 | MIT-CMU and included codec notices |
| ReportLab | 5.0.1 | BSD-style license and font/resource notices |
| pypdf | 6.19.0 | BSD-3-Clause |
| PySide6-Essentials / shiboken6 / Qt | 6.12.0 | LGPL-3.0 option for the used dynamically loaded libraries, plus module-specific/third-party notices |
| charset-normalizer | 3.5.2 | MIT |
| FFmpeg and FFprobe | 8.1.2 | LGPL-2.1-or-later, separate executables; built without GPL, nonfree, version3 or autodetected external libraries |

This application uses dynamically loaded Qt/PySide6 libraries under the LGPLv3 and separate FFmpeg/FFprobe tools under the LGPLv2.1 or later. You may modify and replace those components and reverse engineer the application as needed to debug your modifications. See `SOURCE_AND_REPLACEMENT.md` for source archives, exact build settings and installation/replacement instructions. Original LGPL/GPL and third-party license texts are included with the sources and in `licenses/`.

Our image-only OpenCV build has no VideoCapture, videoio, FFmpeg, x264 or x265 linkage. Video decoding is performed exclusively by the separately supplied FFmpeg/FFprobe executables. The PyPI macOS 5.0.0.93 wheel was inspected and contained a GPL-configured FFmpeg; it is not the OpenCV wheel distributed in this release.

The matching FFmpeg, Qt, Qt for Python and image-only OpenCV sources and build records accompany the release at https://github.com/juhwan0628/slide-extractor-gui/releases. Build provenance and the observed input inventory are provided in `build-manifest.json` and `SBOM.json`. The source tree and build instructions for the app are in the same repository.

## Build tools

PyInstaller is a build tool under GPL with the bootloader exception; this does not change the application's MIT license. Inno Setup is used to create the Windows installer and has its own license. Compiler tools and development tests are not application runtime components.

qpdf is not bundled. PyMuPDF, SciPy, scikit-image and pytest are excluded from the runtime bundle.

OpenCV image-only builds apply `opencv-image-only.patch` to omit development-only typing metadata (whose upstream generator assumes the full module set) and the unused Windows videoio helper from wheel packaging. The exact original source archive, patch and patch SHA-256 are supplied together. No image-processing C++ code is modified.

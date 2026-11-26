Pinned Carlito Regular/Bold (OFL) for browser pages declaring Calibri.

Dockerfile sets FONTCONFIG_FILE to this directory's fonts.conf. Local browser
jobs must use that same environment setting. The relative font directory keeps
the configuration portable. HTML and site CSS remain unchanged.

manifest.json records source and SHA256. This environment was checked on all
six Alumil sample product pages, contacts and the negative catalogue page.
The build configuration does not imply that running containers have been rebuilt.

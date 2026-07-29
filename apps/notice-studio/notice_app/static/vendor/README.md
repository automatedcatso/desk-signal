# Local vendor assets (offline)

The portal runs fully offline on local machines, so third-party assets are
loaded from here instead of a CDN. Drop the following files into place once
(copy from a machine with internet, or from an existing portal install):

```
vendor/
  bootstrap/
    bootstrap.min.css          (Bootstrap 5.3.x)
    bootstrap.bundle.min.js    (Bootstrap 5.3.x bundle, includes Popper)
  bootstrap-icons/
    bootstrap-icons.css        (Bootstrap Icons 1.11.x)
    fonts/
      bootstrap-icons.woff
      bootstrap-icons.woff2
```

Download links (Bootstrap 5.3.0):
- CSS:  https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css
- JS:   https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js
- Icons: https://github.com/twbs/icons/releases (font/bootstrap-icons.css + fonts/)

Keep the folder/file names exactly as above; base.html references these paths.

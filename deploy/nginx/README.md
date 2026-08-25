# deploy/nginx/

The TLS front door, copied off the box. `/etc/nginx/sites-available/stockhunt` (symlinked
into `sites-enabled/`) and the one `conf.d/` snippet it depends on.

    stockhunt.conf            -> /etc/nginx/sites-available/stockhunt
    websocket_upgrade.conf    -> /etc/nginx/conf.d/websocket_upgrade.conf

**These are copies, not the deployment.** Nothing applies them; `autodeploy.sh` restarts
the API and the board and never touches nginx. Editing a file here changes nothing until
somebody puts it on the box and runs `nginx -t && systemctl reload nginx`. They are kept
in the repo for the reason the rest of `deploy/` is — so a change to them can be read,
reviewed and blamed — and because the alternative had already cost something.

## What the alternative cost

`gzip_types` was written against **nginx's** spelling of a JavaScript MIME type and the
responses are labelled by **Python's**. `api_board._serve` calls `mimetypes.guess_type`,
and Python 3.12 answers `text/javascript` for `.js` where nginx's `mime.types` says
`application/javascript`. The list matched the CSS and the JSON and skipped the two
biggest files on the page:

| file | shipped | would gzip to | was compressed |
|---|---|---|---|
| `data.js` | 4,156,352 | 585,080 | no |
| `app.js` | 263,788 | 87,172 | no |
| `app.css` | 40,757 | 12,342 | yes |
| `live.json` | 597,076 | 47,062 | yes |

About 3.7 MB of avoidable transfer on every cold load, and **nothing anywhere logged it**,
because an uncompressed response is a correct response. It surfaced only by reading byte
counts in `/var/log/nginx/access.log` against the sizes on disk.

The lesson generalises past this one line: **a rule keyed on a value another process
chooses is only as right as that process's spelling of it.** If the upstream ever stops
being Python — or Python changes its mind again, which is what happened here — the list
has to be re-checked rather than assumed.

## Checking it, without waiting for a reader

An unauthenticated request cannot reach `/data.js`, so the compression cannot be tested
from outside the login. Point a throwaway upstream that sets the *same* content type at a
throwaway location, and compare:

```bash
curl -s -o /dev/null -w '%{size_download}\n' -H 'Accept-Encoding: identity' URL
curl -s -D - -o /dev/null -H 'Accept-Encoding: gzip' URL | grep -i content-encoding
```

Afterwards the honest end-to-end check is the access log: `data.js` should appear at
roughly 585 KB, not 4.2 MB.

## Two things deliberately left alone

* **No `http2`.** Worth turning on — six connections and no header compression is a real
  cost at 200 ms — but it is a separate change with its own way of going wrong.
* **`proxy_buffering off` is on every location**, not just `/ws`. It is there for the
  socket, which must not be buffered; the static files under `/` pay for it.

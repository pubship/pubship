"""Account pages sharing the companion website's brand, without remote assets.

Original PubShip wordmark and shared website palette (AGPL-3.0-only).
"""

from starlette.responses import HTMLResponse

SECURE_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    # no-referrer produces Origin:null on real HTML POSTs. This retains the
    # exact-origin CSRF check while withholding cross-origin referrers.
    "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
    # WebKit checks the form's 303 destination, not only its initial action.
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; "
        "form-action 'self' https://accounts.google.com; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
}

STYLE = """
:root{--paper:#f7f8f5;--ink:#182d28;--muted:#58665f;--green:#23584a;
--line:#dce2db;--mono:ui-monospace,SFMono-Regular,Consolas,monospace;
font-family:Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
color:var(--ink);background:var(--paper);font-synthesis:none;color-scheme:light}
*{box-sizing:border-box}body{margin:0;min-width:0;line-height:1.6}
a{color:var(--green);text-underline-offset:4px}button,input{font:inherit}
::selection{background:#cbe8d6;color:#102c22}
a:focus-visible,button:focus-visible,input:focus-visible{outline:3px solid #387e65;
outline-offset:4px;border-radius:3px}
.frame{width:min(100% - 48px,1120px);margin-inline:auto}
.header{min-height:88px;display:flex;align-items:center;justify-content:space-between;
gap:20px;border-bottom:1px solid var(--line)}
.wordmark{display:inline-flex;align-items:center;gap:9px;font-size:23px;
letter-spacing:-1px;font-weight:720;color:var(--ink);text-decoration:none}
.wordmark svg{flex-shrink:0}.wordmark span{font-weight:430;color:var(--muted)}
.source{font-size:14px;color:var(--muted);padding:10px 0}
.skip{position:fixed;left:16px;top:12px;transform:translateY(-180%);z-index:2;
background:var(--ink);color:white;padding:12px 18px;border-radius:5px}
.skip:focus{transform:translateY(0)}
main{position:relative;width:min(100% - 48px,640px);margin:64px auto 40px;
isolation:isolate;outline:none}
main:before{content:"";position:absolute;z-index:-1;inset:-40px 0 30%;
background:radial-gradient(ellipse,#d7ecd580,transparent 70%);pointer-events:none}
.eyebrow{font:11px/1.6 var(--mono);letter-spacing:1.8px;text-transform:uppercase;
color:#53665b;margin:0 0 18px}
.panel{background:#fff;border:1px solid var(--line);border-radius:12px;
padding:36px;box-shadow:0 12px 44px #183b3507}
h1{font-size:clamp(28px,4vw,38px);line-height:1.18;letter-spacing:-1.3px;
font-weight:570;margin:0 0 20px;text-wrap:balance}
p{margin:0 0 20px;color:var(--muted);overflow-wrap:anywhere}
strong{font-weight:650;color:var(--ink)}
.request{padding:18px 20px;margin:24px 0;background:var(--paper);
border:1px solid var(--line);border-radius:7px}
.request p{margin:0 0 10px}.request p:last-child{margin-bottom:0}
.request small{margin:6px 0 0}.destination{display:block;margin-top:8px;
font:12px/1.6 var(--mono);color:var(--ink);overflow-wrap:anywhere}
form{margin-top:28px}label{display:block;color:var(--ink);font-size:15px;font-weight:550}
input[type=text]{display:block;width:100%;min-height:48px;padding:11px 13px;
margin-top:8px;border:1px solid #748579;border-radius:5px;background:#fff;
color:var(--ink);font-size:16px;line-height:1.5}
input::placeholder{color:var(--muted);opacity:1}
small{display:block;font-size:13px;line-height:1.6;color:var(--muted);font-weight:400;
margin-top:7px;overflow-wrap:anywhere}
fieldset{margin:28px 0;padding:0;border:0;min-width:0}
legend{font-size:15px;font-weight:550;padding:0;margin-bottom:8px}
fieldset label{display:flex;gap:12px;align-items:flex-start;min-height:44px;
padding:10px 0;cursor:pointer;font-weight:450;line-height:1.5}
input[type=checkbox]{width:20px;height:20px;flex-shrink:0;margin:1px 0 0;
accent-color:var(--green);cursor:pointer}
fieldset>small{margin:0 0 8px 32px}
.capability-choice{padding:16px 0;border-bottom:1px solid var(--line);min-width:0}
.capability-choice:last-child{border-bottom:0}
.capability-choice label{overflow-wrap:anywhere}
.capability-choice .capability-app-label{display:block;min-height:0;padding:8px 0 0;
font-weight:550}
.privacy-note{font-size:13px;line-height:1.7;margin:28px 0 20px;
padding-top:20px;border-top:1px solid var(--line)}
button{display:flex;align-items:center;justify-content:center;width:100%;
min-height:50px;padding:13px 20px;border:1px solid transparent;border-radius:5px;
background:var(--ink);color:#fff;font-size:15px;font-weight:550;cursor:pointer;
transition:background .18s ease}
button:hover{background:#305044}button:active{background:var(--green)}
.footer{width:min(100% - 48px,640px);margin:0 auto 40px;font-size:13px;
display:flex;gap:12px 24px;justify-content:space-between;flex-wrap:wrap;color:var(--muted)}
.footer nav{display:flex;gap:20px;flex-wrap:wrap}.footer a{color:var(--muted)}
@media(max-width:480px){.frame{width:calc(100% - 32px)}.header{min-height:72px}
main{width:calc(100% - 32px);margin-top:32px}.panel{padding:24px 20px}
main:before{inset:-24px 0 30%}.request{padding:16px}.footer{width:calc(100% - 40px)}
h1{letter-spacing:-.8px}}
@media(prefers-reduced-motion:reduce){button{transition:none}}
"""

MARK = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="28" height="28" viewBox="0 0 32 32" aria-hidden="true" focusable="false">'
    '<g fill="none" stroke="#202826" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M8 25V7h9a6 6 0 0 1 0 12H8"/><path d="M17 25h8"/></g></svg>'
)


def page(content, status=200):
    """Wrap trusted HTML fragments; callers must escape variable content."""
    return HTMLResponse(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="theme-color" content="#f7f8f5">'
        '<meta name="robots" content="noindex,nofollow">'
        "<title>Connect your account · PubShip</title><style>"
        + STYLE
        + '</style></head><body><a class="skip" href="#main">Skip to connection</a>'
        '<header class="header frame"><a class="wordmark" href="https://pubship.dev/" '
        'aria-label="PubShip home">' + MARK + "<div>PubShip</div></a>"
        '<a class="source" href="https://github.com/pubship/pubship">GitHub</a>'
        '</header><main id="main" tabindex="-1">'
        '<p class="eyebrow">Your account. Your apps.</p><section class="panel">'
        + content
        + '</section></main><footer class="footer"><span>Independent. Open source.</span>'
        '<nav aria-label="Legal"><a href="https://pubship.dev/privacy">Privacy</a>'
        '<a href="https://pubship.dev/terms">Terms</a></nav></footer></body></html>',
        status_code=status,
        headers=SECURE_HEADERS,
    )

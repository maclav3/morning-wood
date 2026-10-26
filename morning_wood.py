#!/usr/bin/env python3
"""morning-wood: a weekday morning brief from Jira and your recent Claude Code sessions.

Claude runs read-only. It gets no built-in tools, only a few Jira read tools.
This script reads the session transcripts itself and passes a short digest in the prompt.
The brief is rendered to HTML, and a desktop notification opens it.
"""

import sys

if sys.version_info < (3, 11):
    sys.exit("morning-wood needs Python 3.11 or later.")

import argparse
import base64
import hashlib
import html
import json
import os
import plistlib
import re
import shutil
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from string import Template

APP = "morning-wood"
TITLE = "Morning brief"
HERE = Path(__file__).resolve().parent
HOME = Path.home()
IS_MAC = sys.platform == "darwin"

XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config"))
CONFIG_DIR = XDG_CONFIG / APP
CONFIG_FILE = CONFIG_DIR / "config.toml"
CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR", HOME / ".claude"))

# Places where installers often put claude and pandoc. A scheduler may start us with a short PATH.
EXTRA_PATH = [HOME / ".local/bin", HOME / ".claude/local", Path("/opt/homebrew/bin"), Path("/usr/local/bin")]

# Read-only tools of the Atlassian remote MCP server (https://mcp.atlassian.com/v1/mcp).
DEFAULT_JIRA_TOOLS = [
    "getAccessibleAtlassianResources",
    "atlassianUserInfo",
    "searchJiraIssuesUsingJql",
    "getJiraIssue",
]

MAX_PROMPTS = 8
MAX_REPLIES = 3
PROMPT_CHARS = 300
REPLY_CHARS = 600
DIGEST_CHARS = 80_000


@dataclass
class Config:
    mcp_server: str = "atlassian"
    jira_tools: list[str] = field(default_factory=lambda: list(DEFAULT_JIRA_TOOLS))
    workdir: Path = HOME
    output_dir: Path = HOME / "briefs"
    workdays_back: int = 3
    model: str = ""


def load_config() -> Config:
    cfg = Config()
    if not CONFIG_FILE.exists():
        return cfg
    data = tomllib.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    for key, value in data.items():
        default = getattr(cfg, key, None)
        if default is None:
            sys.exit(f"{CONFIG_FILE}: unknown key '{key}'")
        expected = str if isinstance(default, Path) else type(default)
        if not isinstance(value, expected) or (expected is list and not all(isinstance(v, str) for v in value)):
            sys.exit(f"{CONFIG_FILE}: '{key}' must be a {expected.__name__}")
        if isinstance(default, Path):
            # "~/x" and absolute paths stay as they are; a relative path is relative to home.
            value = HOME / Path(value).expanduser()
        setattr(cfg, key, value)
    return cfg


def which(name: str) -> str | None:
    path = os.pathsep.join([os.environ.get("PATH", ""), *map(str, EXTRA_PATH)])
    return shutil.which(name, path=path)


def require(name: str) -> str:
    found = which(name)
    if not found:
        raise RuntimeError(f"'{name}' was not found on PATH.")
    return found


# --- Session digest ---------------------------------------------------------------------------


def workdays_before(day: date, count: int) -> date:
    while count > 0:
        day -= timedelta(days=1)
        if day.weekday() < 5:
            count -= 1
    return day


def text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


def clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def session_digest(path: Path, since: datetime) -> str | None:
    """Summarise one transcript: its title, last prompts and last replies since `since`.

    The transcript format is internal to Claude Code. Unknown records are skipped.
    """
    titles: dict[str, str] = {}
    cwd = branch = ""
    prompts: list[str] = []
    replies: list[str] = []
    with path.open(encoding="utf-8", errors="replace") as lines:
        for line in lines:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            kind = rec.get("type")
            if kind in ("custom-title", "ai-title"):
                titles[kind] = rec.get("customTitle") or rec.get("aiTitle") or ""
                continue
            if kind not in ("user", "assistant") or rec.get("isSidechain") or rec.get("isMeta"):
                continue
            try:
                stamp = datetime.fromisoformat(rec["timestamp"])
            except (KeyError, TypeError, ValueError):
                continue
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            if stamp < since:
                continue
            text = text_of((rec.get("message") or {}).get("content", "")).strip()
            # Skip slash-command echoes, system reminders and other wrapped content.
            if not text or text.startswith("<"):
                continue
            cwd = rec.get("cwd") or cwd
            branch = rec.get("gitBranch") or branch
            if kind == "user":
                prompts.append(clip(text, PROMPT_CHARS))
            else:
                replies.append(clip(text, REPLY_CHARS))

    if not prompts and not replies:
        return None
    title = titles.get("custom-title") or titles.get("ai-title") or path.stem[:8]
    out = [f"## {title}", f"project: {cwd}  branch: {branch}", "user asked:"]
    out += [f"- {p}" for p in prompts[-MAX_PROMPTS:]]
    out.append("assistant said last:")
    out += [f"- {r}" for r in replies[-MAX_REPLIES:]]
    return "\n".join(out)


def build_digest(since: datetime) -> str:
    cutoff = since.timestamp()
    files = sorted(
        (p for p in (CLAUDE_HOME / "projects").glob("*/*.jsonl") if p.stat().st_mtime >= cutoff),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    parts, size = [], 0
    for path in files:
        part = session_digest(path, since)
        if part is None:
            continue
        if size + len(part) > DIGEST_CHARS:
            parts.append("(older sessions left out: digest size limit)")
            break
        parts.append(part)
        size += len(part)
    return "\n\n".join(parts) or "(no sessions)"


def build_prompt(cfg: Config, now: datetime) -> str:
    since = datetime.combine(workdays_before(now.date(), cfg.workdays_back), datetime.min.time()).astimezone()
    override = CONFIG_DIR / "prompt.md"
    template = (override if override.exists() else HERE / "prompt.md").read_text(encoding="utf-8")
    return Template(template).safe_substitute(
        today=now.strftime("%A %d %B %Y"),
        now=now.strftime("%H:%M"),
        cutoff=since.date().isoformat(),
        digest=build_digest(since),
    )


# --- Claude ------------------------------------------------------------------------------------


def tool_prefix(server: str) -> str:
    # Claude Code names MCP tools mcp__<server>__<tool>, with other characters replaced by "_".
    return "mcp__" + re.sub(r"[^A-Za-z0-9_-]", "_", server) + "__"


def run_claude(cfg: Config, prompt: str) -> str:
    if not cfg.workdir.is_dir():
        raise RuntimeError(f"workdir does not exist: {cfg.workdir}")
    cmd = [
        require("claude"), "-p",
        "--output-format", "json",
        # dontAsk denies every tool that is not in --allowedTools, whatever the settings say.
        "--permission-mode", "dontAsk",
        # No built-in tools: no file reads, no writes, no shell.
        "--tools", "",
        "--no-session-persistence",
    ]
    if cfg.model:
        cmd += ["--model", cfg.model]
    cmd += ["--allowedTools", *(tool_prefix(cfg.mcp_server) + t for t in cfg.jira_tools)]
    res = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=cfg.workdir, timeout=1800)
    if res.returncode != 0:
        raise RuntimeError(f"claude exited {res.returncode}: {(res.stderr or res.stdout).strip()[-500:]}")
    data = json.loads(res.stdout)
    if data.get("is_error"):
        raise RuntimeError(f"claude reported an error: {str(data.get('result'))[:500]}")
    return data["result"]


# --- HTML --------------------------------------------------------------------------------------

DARK_VARS = """
  color-scheme: dark;
  --bg1: #2a1f3d; --bg2: #1c1f3a; --bg3: #102a33;
  --tile: #23233a; --fg: #ecebf5; --soft: #a9a7c0; --line: #36364f;
  --shadow: 0 8px 24px rgba(0, 0, 0, .35);
  --attention: #ff8a65; --attention-tint: #3a2730;
  --jira: #6ea2ff; --jira-tint: #22304f;
  --sessions: #3dd6a8; --sessions-tint: #1b3a37;
"""

CSS = """
:root {
  color-scheme: light;
  --bg1: #fff1e0; --bg2: #ffe4ec; --bg3: #e3f0ff;
  --tile: #ffffff; --fg: #26243a; --soft: #6c6a80; --line: #ece8f2;
  --shadow: 0 8px 24px rgba(80, 60, 120, .10);
  --attention: #f2603d; --attention-tint: #fff0eb;
  --jira: #2f6fe4; --jira-tint: #eaf1ff;
  --sessions: #0fa37f; --sessions-tint: #e6f8f2;
}
/* The theme switch sets data-theme on <html>. Without it, the system theme decides. */
:root[data-theme="dark"] { __DARK_VARS__ }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { __DARK_VARS__ } }
.theme-switch { position: fixed; top: 16px; right: 20px; display: flex; gap: 2px; padding: 3px;
  background: var(--tile); border: 1px solid var(--line); border-radius: 999px;
  box-shadow: var(--shadow); opacity: .55; transition: opacity .15s; z-index: 1; }
.theme-switch:hover, .theme-switch:focus-within { opacity: 1; }
.theme-switch button { border: 0; background: none; color: var(--soft); cursor: pointer;
  width: 30px; height: 26px; border-radius: 999px; display: grid; place-items: center; }
.theme-switch svg { width: 15px; height: 15px; fill: none; stroke: currentColor; stroke-width: 2;
  stroke-linecap: round; }
.theme-switch button[aria-pressed="true"] { background: var(--line); color: var(--fg); }
/* Pandoc's default style sets body max-width: 36em. Remove it to use the full screen. */
html { background: linear-gradient(135deg, var(--bg1), var(--bg2) 50%, var(--bg3)) fixed; min-height: 100%; }
body { background: transparent; color: var(--fg); margin: 0; max-width: none; padding: 0;
  font: 16px/1.55 -apple-system, "Segoe UI", Ubuntu, Cantarell, sans-serif; }
main { padding: 32px 32px 64px; }
h1 { font-size: 34px; margin: 0 0 8px; width: fit-content;
  background: linear-gradient(90deg, #f2603d, #e0407e 45%, #7b5cff);
  -webkit-background-clip: text; background-clip: text; color: transparent; }
/* Pandoc --section-divs: the h1 section holds one section per h2. Each h2 section is a tile. */
section.level1 { display: grid; gap: 24px; grid-template-columns: repeat(3, 1fr); align-items: start; }
@media (max-width: 1200px) { section.level1 { grid-template-columns: 1fr; } }
section.level1 > :not(section) { grid-column: 1 / -1; margin: 0; }
section.level2 { --c: var(--jira); --tint: var(--jira-tint);
  background: var(--tile); border-radius: 16px; border-top: 6px solid var(--c);
  box-shadow: var(--shadow); padding: 20px 24px; min-width: 0; overflow-wrap: anywhere; }
#needs-attention-today { --c: var(--attention); --tint: var(--attention-tint); }
#recent-sessions { --c: var(--sessions); --tint: var(--sessions-tint); }
#needs-attention-today h2::before { content: "🔥 "; }
#jira h2::before { content: "📋 "; }
#recent-sessions h2::before { content: "🧭 "; }
h2 { font-size: 20px; margin: 0 0 12px; padding-bottom: 8px; color: var(--c);
  border-bottom: 2px solid var(--tint); }
h3 { font-size: 13px; margin: 20px 0 8px; color: var(--c); text-transform: uppercase;
  letter-spacing: .06em; }
ul { padding-left: 20px; margin: 6px 0; }
li { margin: 6px 0; }
li::marker { color: var(--c); }
strong { color: var(--c); }
a { color: var(--c); font-weight: 600; text-decoration: none; }
a:hover { text-decoration: underline; }
/* Jira keys at the start of a line look like small badges. */
#jira li > a:first-child { background: var(--tint); padding: 1px 8px; border-radius: 999px;
  font-size: 14px; white-space: nowrap; }
code { font: 14px ui-monospace, "JetBrains Mono", monospace; background: var(--tint);
  color: var(--fg); padding: 1px 5px; border-radius: 5px; }
h3 code { text-transform: none; letter-spacing: 0; background: none; padding: 0; color: var(--c); }
em { color: var(--soft); }
""".replace("__DARK_VARS__", DARK_VARS)

THEME_SWITCH = """<nav class="theme-switch" aria-label="Colour theme">
<button type="button" data-theme="light" title="Light"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1L7 17M17 7l2.1-2.1"/></svg></button>
<button type="button" data-theme="auto" title="Auto (system)"><svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/></svg></button>
<button type="button" data-theme="dark" title="Dark"><svg viewBox="0 0 24 24"><path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/></svg></button>
</nav>"""

# Runs in <head>, so a saved theme applies before the first paint.
# localStorage can be blocked for file:// pages; the page then just follows the system.
THEME_JS = """
(function () {
  var KEY = "morning-wood-theme";
  var theme = "auto";
  try { theme = localStorage.getItem(KEY) || "auto"; } catch (e) {}
  function apply() {
    var root = document.documentElement;
    if (theme === "auto") { delete root.dataset.theme; } else { root.dataset.theme = theme; }
    document.querySelectorAll(".theme-switch button").forEach(function (b) {
      b.setAttribute("aria-pressed", String(b.dataset.theme === theme));
    });
  }
  apply();
  document.addEventListener("DOMContentLoaded", function () {
    apply();
    document.querySelectorAll(".theme-switch button").forEach(function (b) {
      b.addEventListener("click", function () {
        theme = b.dataset.theme;
        try {
          if (theme === "auto") { localStorage.removeItem(KEY); } else { localStorage.setItem(KEY, theme); }
        } catch (e) {}
        apply();
      });
    });
  });
})();
"""


# Pandoc's own Markdown reader, close to GitHub's, with the risky extensions off. Raw HTML and
# "{...}" attributes in the model's output become text, a "---" block is never metadata, "@name"
# is not a citation, "~a~" and "^a^" are not sub- or superscript, and "$5 … $6" is not maths.
# (The gfm reader ignores -raw_html in some Pandoc versions.) blank_before_header is off, so a
# heading right after a text line is still a heading.
PANDOC_READER = "markdown" + "".join(f"-{ext}" for ext in (
    "raw_html", "raw_attribute", "raw_tex", "link_attributes", "header_attributes",
    "fenced_code_attributes", "inline_code_attributes", "bracketed_spans", "fenced_divs",
    "yaml_metadata_block", "pandoc_title_block", "citations", "subscript", "superscript",
    "tex_math_dollars", "smart", "blank_before_header",
)) + "+lists_without_preceding_blankline+autolink_bare_uris"


def content_security_policy() -> str:
    # Only our own inline script may run. The page loads nothing from the network, not even images.
    digest = base64.b64encode(hashlib.sha256(THEME_JS.encode()).digest()).decode()
    return f"default-src 'none'; style-src 'unsafe-inline'; script-src 'sha256-{digest}'"


def render_html(markdown: str, out: Path, today: date) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".html", encoding="utf-8") as head:
        head.write(f"<style>{CSS}</style>")
        head.flush()
        subprocess.run(
            [require("pandoc"), "-f", PANDOC_READER, "-t", "html5", "--standalone", "--section-divs",
             "--metadata", f"pagetitle={TITLE} {today}", "-H", head.name, "-o", str(out)],
            input=markdown, text=True, check=True,
        )
    page = out.read_text(encoding="utf-8")
    # Keep only web links. A javascript: or file: link from Jira text would otherwise be clickable.
    page = re.sub(r'href="(?!https?://|#)[^"]*"', 'href="#"', page)
    # Added after Pandoc, because Pandoc re-indents its header and the CSP hash needs the exact script.
    csp = f'<meta http-equiv="Content-Security-Policy" content="{content_security_policy()}">'
    page = page.replace("<head>", f"<head>\n{csp}", 1)
    page = page.replace("</head>", f"<script>{THEME_JS}</script>\n</head>", 1)
    page = page.replace("<body>", f"<body>\n{THEME_SWITCH}\n<main>", 1)
    page = page.replace("</body>", "</main>\n</body>", 1)
    out.write_text(page, encoding="utf-8")


# --- Notifications -----------------------------------------------------------------------------


def notification_lines(markdown: str) -> list[str]:
    """Return the first 3 bullets as plain text."""
    bullets = [line[2:] for line in markdown.splitlines() if line.startswith("- ")][:3]
    lines = []
    for text in bullets:
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)  # Keep link text, drop the URL.
        lines.append("• " + clip(text.replace("**", "").replace("`", ""), 140))
    return lines or ["Click to open."]


def open_brief(path: Path) -> None:
    opener = "open" if IS_MAC else which("sensible-browser") or which("xdg-open")
    if not opener:
        print(f"No browser opener found. Open the brief yourself: {path}", file=sys.stderr)
        return
    cmd = [opener, path.as_uri()]
    # Inside a systemd service, a separate scope keeps the browser alive after the service exits.
    if not IS_MAC and os.environ.get("INVOCATION_ID") and which("systemd-run"):
        cmd = ["systemd-run", "--user", "--scope", "--quiet", *cmd]
    subprocess.Popen(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def notify(title: str, lines: list[str], open_path: Path | None = None, urgent: bool = False) -> None:
    if IS_MAC:
        notify_macos(title, lines, open_path)
    else:
        notify_linux(title, lines, open_path, urgent)


def notify_linux(title: str, lines: list[str], open_path: Path | None, urgent: bool) -> None:
    exe = which("notify-send")
    if not exe:
        print(f"{title}: notify-send not found, no notification shown.", file=sys.stderr)
        return
    # Most notification servers read the body as markup.
    body = html.escape("\n".join(lines), quote=False)
    base = [exe, f"--app-name={TITLE}", f"--urgency={'critical' if urgent else 'normal'}"]
    if open_path is None:
        # "--" stops option parsing: a body that starts with "-" is text, not a flag.
        subprocess.run([*base, "--", title, body], check=False)
        return
    # --wait keeps notify-send running until the notification is clicked or closed.
    cmd = [*base, "--icon=text-html", "--action=default=Open", "--wait", "--", title, body]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=8 * 3600)
    except subprocess.TimeoutExpired:
        return
    if res.returncode != 0:
        print(f"notify-send failed: {res.stderr.strip()}", file=sys.stderr)
    elif res.stdout.strip() == "default":
        open_brief(open_path)


def notify_macos(title: str, lines: list[str], open_path: Path | None) -> None:
    body = "\n".join(lines)
    exe = which("terminal-notifier")
    if exe:
        cmd = [exe, "-title", title, "-message", body, "-group", APP]
        if open_path is not None:
            cmd += ["-open", open_path.as_uri()]
        subprocess.run(cmd, check=False)
        return
    # osascript cannot react to a click. Install terminal-notifier for that.
    script = ["on run argv", "display notification (item 2 of argv) with title (item 1 of argv)", "end run"]
    cmd = ["osascript", *[a for line in script for a in ("-e", line)], title, body]
    subprocess.run(cmd, check=False)


# --- Scheduler ---------------------------------------------------------------------------------

SYSTEMD_DIR = XDG_CONFIG / "systemd/user"
LAUNCHD_LABEL = f"local.{APP}"
LAUNCHD_PLIST = HOME / "Library/LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
MAC_LOG = HOME / "Library/Logs" / f"{APP}.log"


def parse_time(text: str) -> tuple[int, int]:
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", text)
    if not match:
        raise argparse.ArgumentTypeError("use HH:MM, for example 07:37")
    return int(match[1]), int(match[2])


def systemd_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def install(hour: int, minute: int) -> None:
    script = Path(__file__).resolve()
    if not CONFIG_FILE.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy(HERE / "config.example.toml", CONFIG_FILE)
        print(f"Wrote {CONFIG_FILE}. Check mcp_server and workdir in it.")
    # Keep the PATH of this shell, so the scheduler finds claude and pandoc.
    path = os.environ.get("PATH", "")
    if IS_MAC:
        LAUNCHD_PLIST.parent.mkdir(parents=True, exist_ok=True)
        MAC_LOG.parent.mkdir(parents=True, exist_ok=True)
        plist = {
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": [sys.executable, str(script), "run"],
            # launchd weekdays: 1 is Monday, 5 is Friday. A run missed during sleep starts on wake.
            "StartCalendarInterval": [{"Weekday": d, "Hour": hour, "Minute": minute} for d in range(1, 6)],
            "EnvironmentVariables": {"PATH": path},
            "StandardOutPath": str(MAC_LOG),
            "StandardErrorPath": str(MAC_LOG),
        }
        with LAUNCHD_PLIST.open("wb") as f:
            plistlib.dump(plist, f)
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", domain, str(LAUNCHD_PLIST)], capture_output=True)
        subprocess.run(["launchctl", "bootstrap", domain, str(LAUNCHD_PLIST)], check=True)
        print(f"Installed {LAUNCHD_PLIST}: weekdays at {hour:02d}:{minute:02d}. Log: {MAC_LOG}")
        return

    SYSTEMD_DIR.mkdir(parents=True, exist_ok=True)
    (SYSTEMD_DIR / f"{APP}.service").write_text(
        "[Unit]\nDescription=Claude morning brief\n\n"
        "[Service]\nType=oneshot\n"
        f"Environment={systemd_quote('PATH=' + path)}\n"
        f"ExecStart={systemd_quote(sys.executable)} {systemd_quote(str(script))} run\n",
        encoding="utf-8",
    )
    (SYSTEMD_DIR / f"{APP}.timer").write_text(
        "[Unit]\nDescription=Weekday Claude morning brief\n\n"
        # Persistent: a run missed while the computer was off or asleep starts at the next boot or wake.
        f"[Timer]\nOnCalendar=Mon..Fri {hour:02d}:{minute:02d}\nPersistent=true\n\n"
        "[Install]\nWantedBy=timers.target\n",
        encoding="utf-8",
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", f"{APP}.timer"], check=True)
    subprocess.run(["systemctl", "--user", "list-timers", f"{APP}.timer", "--no-pager"], check=False)


def uninstall() -> None:
    if IS_MAC:
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(LAUNCHD_PLIST)], capture_output=True)
        LAUNCHD_PLIST.unlink(missing_ok=True)
    else:
        subprocess.run(["systemctl", "--user", "disable", "--now", f"{APP}.timer"], check=False)
        for suffix in ("service", "timer"):
            (SYSTEMD_DIR / f"{APP}.{suffix}").unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    print(f"Removed the schedule. Your config ({CONFIG_DIR}) and briefs are kept.")


# --- Commands ----------------------------------------------------------------------------------


def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    now = datetime.now().astimezone()
    md_path = cfg.output_dir / f"{now.date()}.md"
    html_path = cfg.output_dir / f"{now.date()}.html"
    try:
        cfg.output_dir.mkdir(parents=True, exist_ok=True)
        markdown = run_claude(cfg, build_prompt(cfg, now))
        md_path.write_text(markdown, encoding="utf-8")
        render_html(markdown, html_path, now.date())
    except Exception as err:  # Any failure must reach the desktop, not only a log.
        print(f"error: {err}", file=sys.stderr)
        if not args.no_notify:
            notify(f"{TITLE} failed", [clip(str(err), 300)], urgent=True)
        return 1
    print(html_path)
    if args.open:
        open_brief(html_path)
    elif not args.no_notify:
        notify(f"{TITLE} ready", notification_lines(markdown), open_path=html_path)
    return 0


def cmd_check(cfg: Config) -> int:
    ok = True

    def report(good: bool, text: str, required: bool = True) -> None:
        nonlocal ok
        ok = ok and (good or not required)
        print(f"{'✔' if good else ('✘' if required else '–')} {text}")

    report(True, f"Python {sys.version.split()[0]}")
    report(CONFIG_FILE.exists(), f"config: {CONFIG_FILE}", required=False)
    claude = which("claude")
    report(bool(claude), f"claude: {claude or 'not found'}")
    report(bool(which("pandoc")), f"pandoc: {which('pandoc') or 'not found'}")
    if IS_MAC:
        report(bool(which("terminal-notifier")), "terminal-notifier (optional: click to open the brief)", False)
    else:
        report(bool(which("notify-send")), f"notify-send: {which('notify-send') or 'not found'}")
        report(bool(which("systemctl")), "systemctl (needed for install)", False)
    report((CLAUDE_HOME / "projects").is_dir(), f"session transcripts: {CLAUDE_HOME / 'projects'}", False)
    report(cfg.workdir.is_dir(), f"workdir: {cfg.workdir}")
    if claude and cfg.workdir.is_dir():
        res = subprocess.run([claude, "mcp", "get", cfg.mcp_server], capture_output=True, text=True,
                             cwd=cfg.workdir, timeout=120)
        found = res.returncode == 0
        report(found, f'MCP server "{cfg.mcp_server}" seen from {cfg.workdir}')
        if found and "connected" not in res.stdout.lower():
            print("  The server is configured but may not be connected. Run /mcp in claude to log in.")
        if not found:
            print("  Run `claude mcp list` there to see the server names.")
    print(f"Jira tools allowed: {', '.join(tool_prefix(cfg.mcp_server) + t for t in cfg.jira_tools)}")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog=APP, description="A weekday morning brief from Jira and Claude Code sessions.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="write today's brief now")
    run.add_argument("--open", action="store_true", help="open the brief at once instead of notifying")
    run.add_argument("--no-notify", action="store_true", help="only print the path of the brief")
    sub.add_parser("check", help="check the prerequisites")
    sub.add_parser("prompt", help="print the prompt that would be sent to Claude, without sending it")
    inst = sub.add_parser("install", help="run the brief every weekday")
    inst.add_argument("--time", type=parse_time, default=(7, 37), help="local time, HH:MM (default 07:37)")
    sub.add_parser("uninstall", help="remove the schedule")
    args = parser.parse_args()

    cfg = load_config()
    if args.command == "run":
        return cmd_run(cfg, args)
    if args.command == "check":
        return cmd_check(cfg)
    if args.command == "prompt":
        print(build_prompt(cfg, datetime.now().astimezone()))
        return 0
    if args.command == "install":
        install(*args.time)
        return 0
    uninstall()
    return 0


if __name__ == "__main__":
    sys.exit(main())

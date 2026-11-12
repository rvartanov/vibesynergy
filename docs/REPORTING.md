# Reporting a bug or asking for a feature

Both go through [GitHub issues](https://github.com/rvartanov/vibesynergy/issues/new/choose).
You need a (free) GitHub account. In VibeSynergy, **Report a bug** in the footer of every page
(and under Settings → System) opens a new bug report with the version, the page, the error and your browser already filled
in.

The issues are **public**. Leave out health values, names, addresses, tokens and private links.
Dutch or English, both are fine.

## A bug report

Something doesn't work as it should. It's picked up when these are in it (the form asks for them):

| Needed | Why | Example |
|---|---|---|
| What happened | To know what to look for | "Planning shows an empty week" |
| What you expected | Sometimes it works as designed | "My sessions for next week" |
| Steps to make it happen again | Without them it can't be checked or fixed | "Open Planning → Next week" |
| Version | Fixed already, or new? | `0.0.1a1 · cd25eae` (footer, or `vibesynergy version`) |
| How it's installed | Container, uv or source behave differently | "Container on Docker Desktop" |
| Computer and browser | Some bugs only show in one | "macOS 15, Safari 18" |

Helps a lot, when you have it:

- **A screenshot** (drop it in the form). Check it for personal data first.
- **The log**, the last 50 lines: `docker compose logs --tail 50 vibesynergy` (container) or
  `journalctl --user -u vibesynergy -n 50` (Linux service).
- **Once or always?** And did it work before an update?

## A feature request

Something you miss, or an idea. It's weighed when these are in it:

| Needed | Why | Example |
|---|---|---|
| What you want to do, and what stops you now | The problem first: there may be a better answer than the first idea | "After a long ride I want to see my fuelling, but I have to check two apps" |
| Why it matters, and how often | To weigh it against other work | "Every long weekend ride" |
| Where in VibeSynergy | So it reaches the right part | "Coach and training plan" |

Optional: how you picture it (a sketch, an example from another app), and what you do now instead.

## What happens next

1. A new issue is read and labelled within a few days: `bug` or `enhancement`, and a priority
   `P1` (blocks use), `P2` (soon) or `P3` (later).
2. **Something needed is missing**: it gets the label `needs-info` with a question. Without an
   answer within 14 days it is closed. You can reopen it by answering.
3. **Picked up**: the issue links to the change that fixes it, and is closed when the change is
   merged. The next update brings it (`vibesynergy update`, **Update now** in Settings → System,
   or a new container image).
4. **Not doing it**: closed with the reason (for example out of scope, or it can't be done
   with a source's API).

VibeSynergy is alpha and has one maintainer: a complete report is the fastest way to a fix.

## Security problems

Don't open a public issue for a security problem (for example a way past the login). Use
GitHub's private report instead: **Security → Report a vulnerability** on the repository.

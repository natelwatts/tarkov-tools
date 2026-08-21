# Chrome "Aw, Snap!" when opening wiki pages

Status: **not caused by this tool**, and not reproducible on demand. Parked
on 2026-08-21. This is the evidence so far, so it does not have to be
gathered again if it comes back.

## What was seen

Opening a wiki article from the popover loaded the correct page, then the tab
died with:

```
Aw, Snap!  Something went wrong while displaying this webpage.
Error code: STATUS_BREAKPOINT
```

Reloading the tab always worked. The URL and tab title were correct both
times, so nothing was wrong with what was being opened.

## What it is

Chrome writes a minidump per renderer crash to:

```
%LOCALAPPDATA%\Google\Chrome\User Data\Crashpad\reports
```

Three dumps from that afternoon were all **the same crash**:

| dump time | exception | address | module |
|---|---|---|---|
| 17:08 | `0x80000003` STATUS_BREAKPOINT | `0x00007FFB58ED46C7` | `chrome.dll` 151.0.7922.170 |
| 17:17 | `0x80000003` STATUS_BREAKPOINT | `0x00007FFB58ED46C7` | `chrome.dll` 151.0.7922.170 |
| 17:19 | `0x80000003` STATUS_BREAKPOINT | `0x00007FFB58ED46C7` | `chrome.dll` 151.0.7922.170 |

`STATUS_BREAKPOINT` is not memory corruption - it is Chrome deliberately
aborting because one of its own `CHECK()` assertions failed. The identical
faulting address across all three means one specific assertion, not random
instability. Each dump had ~20 loaded modules, which is a sandboxed renderer
process rather than the browser process.

`docs/` has no tooling for this; the dumps were read with a throwaway script
that parses the minidump exception stream (type 6) and module list (type 4)
and maps the faulting address to a module. Worth rewriting if it recurs.

## What it is not

**Not our UI Automation.** The suspicion was that walking Chrome's
accessibility tree for tab reuse (`browser_tabs.find_tab`, a
`FindAll(TreeScope_Subtree, ...)` over every Chrome window) forces Chrome
into full accessibility mode and destabilises renderers. An A/B test opened
the same three wiki pages twice:

- **A** - plain `chrome.exe <url>`, no UI Automation anywhere: **1 crash**
- **B** - same pages, each preceded by the tab-reuse subtree walk: **0 crashes**

The crash happened in the run that never touched UI Automation, and not in
the run that did. That exonerates the traversal.

**Not a particular page, and not fandom.** A follow-up opened three fandom
articles and two control pages (Wikipedia, example.com) twice each - ten
loads, **zero crashes**.

So it is intermittent, and whatever condition triggers the assertion was not
reproduced by simply loading pages.

## Most likely explanation

A bug in that Chrome build. At the time the machine was on
**151.0.7922.170**, while stable had already moved to **152.0.7977.54** with
**151.0.7922.174** available as a patch on the same line.

**First thing to try if it returns: update Chrome.**

## If it comes back

1. Note the time, then list new dumps in `Crashpad\reports`.
2. Parse the exception code, address and module. If the address matches
   `0x00007FFB58ED46C7` in a 151.x `chrome.dll`, it is this same assertion
   and updating Chrome is the fix.
3. If it is a *different* address or module, this write-up does not apply -
   start over.
4. `chrome://crashes` lists the same crashes with upload IDs, which is what
   a Chrome bug report needs.
5. Only if a new A/B test implicates it, revisit `browser_tabs.find_tab`.
   The cheap mitigation there would be narrowing the search from
   `TreeScope_Subtree` to the tab strip, so page content is never walked.

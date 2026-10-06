# The Aither Android app shell

The Android app used to be one full-screen WebView of the website: it opened on whatever the
web root showed, back walked the browser history, and the phone's own settings had no button.
It is now an app with a native frame around AitherOS. Source: `awdk/android/aither/src/...`
`AppTabs.java` (pure rules), `Shell.java` (views), `Ui.java` (brand tokens),
`MainActivity.java` (WebView setup, links), `SettingsActivity.java` (this phone).

## Layouts

Grown-up phone (Home is native, the other tabs are web pages, each its own WebView):

```
+--------------------------------------+      +--------------------------------------+
| [mark] Your apps                     |      | <-  Learn                   (R)  (*) |
| Tap one to open it.                  |      |--------------------------------------|
|                                      |      |                                      |
| +--------+ +--------+ +--------+     |      |   /learn/parent/  (WebView, kept     |
| | Learn  | | Family | | Talk to|     |      |    alive with its own back stack)    |
| |        | |        | | Aither |     |      |                                      |
| +--------+ +--------+ +--------+     |      |                                      |
| | Hearth | | Sprite | |Classrm.|     |      |                                      |
| +--------+ +--------+ +--------+     |      |                                      |
| | Spaces | | Avatar | |AitherOS|     |      |                                      |
| +--------+ +--------+ +--------+     |      |                                      |
| |Control | |Account | |This    |     |      |                                      |
| +--------+ +--------+ |phone   |     |      |                                      |
|--------------------------------------|      |--------------------------------------|
| Home  Learn  Family  Aither  Settings|      | Home  Learn  Family  Aither  Settings|
+--------------------------------------+      +--------------------------------------+
  (R) refresh  (*) gear = Settings             an app with no tab opens in a window:
                                               | <-  Hearth            (R)  (X) |
```

Child phone (the page's `aither.device.child` flag; no Home grid, no grown-up surfaces):

```
+--------------------------------------+
|      Learn                  (R)  (*) |
|--------------------------------------|
|   /learn  (quests, class work)       |
|                                      |
|--------------------------------------|
| Learn  Sprite  Room  My Space  Settings
+--------------------------------------+
  Sprite = /learn/sprite, Room = /hearth#room, My Space = /hearth,
  Settings = the lite screen (screen-off running, updates, family, report)
```

## Rules

| what | rule |
|---|---|
| tabs | grown-up: Home, Learn (`/learn/parent/`), Family (`/hearth/`), Aither (`/chat`), Settings. Child: Learn, Sprite, Room, My Space, Settings. |
| active tab tapped | back to that tab's root, its history cleared |
| Android back | window page -> close window -> page history -> tab root -> Home -> leave the app |
| a link (App Link, `aither://open?url=`, shortcut, notification) | bare origin -> Home; `?app=learn/family/greeter` -> that tab; a path under a tab -> that tab; anything else -> a window. A child's `?app=` ids resolve like the web edge's child table. |
| Settings | the bottom bar, the gear on every top bar, and the "This phone" tile all open it |
| local model pairing | the one-time `#local-pair=` hand-off loads out of sight; it no longer opens the desktop |
| role change | when the page flips the child flag, the launcher shortcuts and the tabs both follow |

Every path is a real page of the web app and every `?app=` id a real OS app; the monorepo test
for the shell fails on one that is not, and `test/AppTabsCheck.java` pins the routing and back
rules on a desktop JVM.

## Look

Colours, radii and spacing are the brand kit's tokens (void `#000103`, surface `#02060D`,
border `#162330`, element cyan `#2AD7D7`, text `#EEEEEE` / `#9BA6B1` / `#69737D`; 16dp
panels, 10dp compact; 4-8-12-16-24 spacing). The mark is the Iris cut, ported to a vector
drawable (`res/drawable/aither_mark.xml`); the launcher icon and shortcut tiles stay generated
from the app icon. Inter is the brand face but Android does not ship it, so the system sans
stands in at Inter's weights.

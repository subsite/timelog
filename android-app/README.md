# Timelog for Android

A small companion app: it shows the running or paused Timelog timer as an ongoing
notification, with Pause/Resume and Stop buttons. On Android 16 and later the notification is a
*Live Update*, so a chip in the status bar shows the ticking time (or the frozen time next to a
pause icon). The app itself has the same timer card and the settings: the Timelog address and an
API key (made in Timelog under Settings → API key; use the same key as your other clients).

It keeps `/api/v1/timer/stream` open in a foreground service, so timers started in the browser
or elsewhere appear at once. While no timer runs it shows a silent, collapsed "No timer running"
notification; you can turn that channel off in the app's notification settings. It starts again
after a reboot.

## Build

In a container (needs only podman and the Android SDK in `~/Android/Sdk`):

```sh
android-app/build.sh
adb install -r android-app/app/build/outputs/apk/release/app-release.apk
```

Or directly:

Needs a JDK 21 (`sudo dnf install java-21-openjdk-devel`) and the Android SDK
(`~/Android/Sdk`; Gradle downloads platform 36 if it is missing).

```sh
cd android-app
echo "sdk.dir=$HOME/Android/Sdk" > local.properties
./gradlew assembleRelease
adb install -r app/build/outputs/apk/release/app-release.apk
```

The release build is signed with `signing.keystore` (gitignored; made with `keytool`, see
`app/build.gradle.kts`). Keep it: Android only installs updates signed with the same key, and
without it you must uninstall the app before installing a new build.

Every build bumps the number in `build-number` (gitignored), which is the version's last part
(shown at the bottom of the app) and the version code.

## Google Play (internal testing)

Installing through Play avoids Play Protect's warning for unknown apps, and updates arrive by
themselves. Build a bundle instead of an APK:

```sh
android-app/build.sh bundle
```

and upload `android-app/app/build/outputs/bundle/release/app-release.aab` in the Play Console:

1. Create the app (package `fi.subsite.timelog`), then under *Test and release → Testing →
   Internal testing* add a tester list with your Google account.
2. Create a release and upload the `.aab`. Accept *Play App Signing*: Google signs what users
   get, and `signing.keystore` becomes the **upload key** (Play remembers it from the first
   upload; later bundles must be signed with it). The upload replaces the app you sideloaded,
   whose key differs, so uninstall that once before installing from Play.
3. Fill in what the console asks before the first release: privacy policy URL, app access,
   data safety, and the declaration for the `specialUse` foreground service (keeping the timer
   stream open to show it in the status bar).
4. Open the tester link on the phone, accept, and install from Play.

The minified build's `app/build/outputs/mapping/release/mapping.txt` can be uploaded with each
release, so crash reports in the console show real names.

On first start allow notifications and, when asked, "Allow running in the background" (turns off
battery optimization so the connection survives Doze). If the status bar chip does not appear on
Android 16, check that *Live updates* is allowed in the app's notification settings.

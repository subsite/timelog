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

On first start allow notifications and, when asked, "Allow running in the background" (turns off
battery optimization so the connection survives Doze). If the status bar chip does not appear on
Android 16, check that *Live updates* is allowed in the app's notification settings.

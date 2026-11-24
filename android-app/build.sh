#!/bin/sh
# Builds the APK in a container (official Gradle image with JDK 21), using the
# Android SDK in ~/Android/Sdk (or $ANDROID_HOME). Gradle downloads missing SDK
# parts (platform 36) into that SDK, and its own cache goes to a podman volume.
# Output: app/build/outputs/apk/release/app-release.apk
set -e
cd "$(dirname "$0")"
SDK="${ANDROID_HOME:-$HOME/Android/Sdk}"
GRADLE=8.14.3
podman run --rm \
    --security-opt label=disable \
    -v "$PWD":/project \
    -v "$SDK":/sdk \
    -v timelog-gradle:/home/gradle/.gradle \
    -e ANDROID_HOME=/sdk \
    -w /project \
    "docker.io/library/gradle:$GRADLE-jdk21" \
    sh -c "[ -f gradlew ] || gradle wrapper --gradle-version $GRADLE
           ./gradlew --no-daemon assembleRelease"
ls -l app/build/outputs/apk/release/app-release.apk

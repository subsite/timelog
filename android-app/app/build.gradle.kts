plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Every build that makes an APK gets the next number (kept in the gitignored file
// build-number), shown as the last part of the version, so you can tell builds apart.
val buildNumberFile = rootProject.file("build-number")
val buildNumber: Int = run {
    val last = buildNumberFile.takeIf { it.exists() }?.readText()?.trim()?.toIntOrNull() ?: 1
    val makesApk = gradle.startParameter.taskNames.any {
        it.contains("assemble", ignoreCase = true) || it.contains("install", ignoreCase = true)
    }
    if (makesApk) (last + 1).also { buildNumberFile.writeText("$it\n") } else last
}

android {
    namespace = "fi.subsite.timelog"
    compileSdk = 36

    defaultConfig {
        applicationId = "fi.subsite.timelog"
        minSdk = 26
        targetSdk = 36
        versionCode = buildNumber
        versionName = "1.0.$buildNumber"
    }

    signingConfigs {
        // A key kept next to the project (gitignored), so every build can update the
        // installed app. Losing it means uninstalling the app once.
        create("local") {
            storeFile = rootProject.file("signing.keystore")
            storePassword = "timelog"
            keyAlias = "timelog"
            keyPassword = "timelog"
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"))
            signingConfig = signingConfigs.getByName("local")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
    }
}

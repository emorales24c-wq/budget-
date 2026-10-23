plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.edwin.livebets"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.edwin.livebets"
        minSdk = 26
        targetSdk = 35
        versionCode = (System.getenv("GITHUB_RUN_NUMBER") ?: "1").toInt()
        versionName = "1.0.${System.getenv("GITHUB_RUN_NUMBER") ?: "0"}"
    }

    // Fixed key so new builds install over the old one
    signingConfigs {
        create("livebets") {
            storeFile = file("livebets.keystore")
            storePassword = "livebets"
            keyAlias = "livebets"
            keyPassword = "livebets"
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("livebets")
        }
    }

    // The app UI is the same page as the web version
    sourceSets["main"].assets.srcDirs("../../live-bets")

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.webkit:webkit:1.12.1")
}

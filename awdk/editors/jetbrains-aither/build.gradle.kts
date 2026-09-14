plugins {
    id("org.jetbrains.kotlin.jvm") version "2.0.21"
    id("org.jetbrains.intellij.platform") version "2.1.0"
}

group = "com.aitherium"
version = "0.1.0"

repositories {
    mavenCentral()
    intellijPlatform {
        defaultRepositories()
    }
}

dependencies {
    intellijPlatform {
        intellijIdeaCommunity("2024.2.2")
        pluginVerifier()
        zipSigner()
        instrumentationTools()
    }
    implementation(kotlin("stdlib"))
}

kotlin {
    jvmToolchain(21)
}

intellijPlatform {
    pluginConfiguration {
        name = "Aither — ACP + A2A integration"
        id = "com.aitherium.aither-jetbrains"
        version = "0.1.0"
        description = "Full A2A + ACP integration from JetBrains. Drives any AitherOS agent (atlas, lyra, demiurge, ...) over the Agent Client Protocol (adk acp serve, stdio JSON-RPC v2) and talks agent-to-agent via A2A. Requires the awdk CLI on PATH."
        vendor {
            name = "Aitherium"
            url = "https://aitherium.com"
        }
    }
    plugins {
        instrumentCode = true
    }
}

tasks {
    patchPluginXml {
        sinceBuild.set("242")
        untilBuild.set("251.*")
    }
    runIde {
        // Default: the IntelliJ IDEA distribution on this machine, if any.
    }
}

#!/usr/bin/env node
/**
 * AitherADK CLI wrapper for npm distribution.
 *
 * This is a thin shim that delegates to the Python `aither` CLI.
 * If Python isn't installed, it provides helpful installation instructions.
 *
 * Install: npm install -g awdk
 * Usage:   aither init my-agent
 */

const { execFileSync, execSync } = require("child_process");
const path = require("path");

function findPython() {
  for (const cmd of ["python3", "python", "py"]) {
    try {
      const version = execFileSync(cmd, ["-c", "import sys; print(sys.version_info[:2])"], {
        encoding: "utf-8",
        stdio: ["pipe", "pipe", "pipe"],
        timeout: 5000,
      }).trim();
      // Check version >= 3.10
      const match = version.match(/\((\d+), (\d+)\)/);
      if (match && (parseInt(match[1]) > 3 || (parseInt(match[1]) === 3 && parseInt(match[2]) >= 10))) {
        return cmd;
      }
    } catch {
      // try next
    }
  }
  return null;
}

function findAitherCli() {
  try {
    const result = execSync("aither --help", {
      encoding: "utf-8",
      stdio: ["pipe", "pipe", "pipe"],
      timeout: 5000,
    });
    if (result.includes("AitherADK")) {
      return "aither";
    }
  } catch {
    // Not installed via pip
  }
  return null;
}

const python = findPython();
if (!python) {
  console.error("Error: Python 3.10+ is required but not found.");
  console.error("");
  console.error("Install Python:");
  console.error("  Windows: winget install Python.Python.3.12");
  console.error("  macOS:   brew install python@3.12");
  console.error("  Linux:   sudo apt install python3.12");
  process.exit(1);
}

// Check if aither CLI is already available (pip install)
const aitherCli = findAitherCli();
if (aitherCli) {
  // Delegate directly
  try {
    execFileSync(aitherCli, process.argv.slice(2), { stdio: "inherit" });
  } catch (e) {
    process.exit(e.status || 1);
  }
} else {
  // Install via pip and then run
  console.log("Installing awdk Python package...");
  try {
    execFileSync(python, ["-m", "pip", "install", "--quiet", "awdk"], {
      stdio: "inherit",
    });
    // Now run
    execFileSync(python, ["-m", "adk.cli"].concat(process.argv.slice(2)), {
      stdio: "inherit",
    });
  } catch (e) {
    console.error("Failed to install awdk. Try: pip install awdk");
    process.exit(1);
  }
}

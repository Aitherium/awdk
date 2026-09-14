#!/usr/bin/env node
/**
 * Post-install script for npm package.
 * Ensures the Python awdk package is installed.
 */

const { execFileSync } = require("child_process");

function findPython() {
  for (const cmd of ["python3", "python", "py"]) {
    try {
      const version = execFileSync(cmd, ["-c", "import sys; print(sys.version_info[:2])"], {
        encoding: "utf-8",
        stdio: ["pipe", "pipe", "pipe"],
        timeout: 5000,
      }).trim();
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

const python = findPython();
if (!python) {
  console.log("");
  console.log("  Note: Python 3.10+ not found. AitherADK requires Python.");
  console.log("  Install Python, then run: pip install awdk");
  console.log("");
  process.exit(0); // Don't fail npm install
}

// Check if already installed
try {
  execFileSync(python, ["-c", "import adk"], {
    stdio: ["pipe", "pipe", "pipe"],
    timeout: 5000,
  });
  console.log("  AitherADK Python package already installed.");
} catch {
  console.log("  Installing AitherADK Python package...");
  try {
    execFileSync(python, ["-m", "pip", "install", "--quiet", "awdk"], {
      stdio: "inherit",
      timeout: 120000,
    });
    console.log("  AitherADK installed successfully.");
  } catch {
    console.log("  Auto-install failed. Run manually: pip install awdk");
  }
}

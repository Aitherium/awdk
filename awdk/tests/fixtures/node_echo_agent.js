// Minimal Node stdio agent for exercising the Supervisor's `node` runtime.
// Reads newline-delimited lines on stdin and echoes them back on stdout.
// Kept dependency-free and tiny so the test needs no npm install.
const args = process.argv.slice(2);
process.stdout.write(`ready:${args.join(",")}\n`);

let buf = "";
process.stdin.on("data", (chunk) => {
  buf += chunk.toString("utf8");
  let idx;
  while ((idx = buf.indexOf("\n")) !== -1) {
    const line = buf.slice(0, idx).trim();
    buf = buf.slice(idx + 1);
    if (line) process.stdout.write(`echo:${line}\n`);
  }
});
process.stdin.on("end", () => process.exit(0));

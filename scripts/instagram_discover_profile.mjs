#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { chromium } from "playwright";

function parseArgs(argv) {
  const args = {
    limit: 30,
    scrolls: 8,
    waitMs: 1800,
    headed: false,
    output: "",
    saveAccount: "",
    python: process.env.PYTHON || path.join(".venv", "Scripts", "python.exe"),
  };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--username") args.username = argv[++index];
    else if (arg === "--profile-url") args.profileUrl = argv[++index];
    else if (arg === "--limit") args.limit = Number(argv[++index]);
    else if (arg === "--scrolls") args.scrolls = Number(argv[++index]);
    else if (arg === "--wait-ms") args.waitMs = Number(argv[++index]);
    else if (arg === "--output") args.output = argv[++index];
    else if (arg === "--save-account") args.saveAccount = argv[++index];
    else if (arg === "--python") args.python = argv[++index];
    else if (arg === "--headed") args.headed = true;
    else if (arg === "--help" || arg === "-h") args.help = true;
    else throw new Error(`Unknown argument: ${arg}`);
  }
  return args;
}

function usage() {
  return `Usage:
  node scripts/instagram_discover_profile.mjs --username meme.ig --limit 30 --output data/candidates/meme-ig-urls.txt
  node scripts/instagram_discover_profile.mjs --username meme.ig --limit 30 --save-account "Test IG"

Options:
  --username NAME       Instagram username, e.g. meme.ig
  --profile-url URL     Full Instagram profile URL
  --limit N             Max unique post/Reel URLs to collect, default 30
  --scrolls N           Max scroll/show-more attempts, default 8
  --wait-ms N           Delay after navigation/scrolls, default 1800
  --output PATH         Write discovered URLs to a text file
  --save-account NAME   Run scripts/instagram_scrape_urls.py and save candidates
  --python PATH         Python executable for --save-account, default .venv\\Scripts\\python.exe
  --headed              Show the browser window`;
}

function profileUrl(args) {
  if (args.profileUrl) return args.profileUrl;
  if (!args.username) throw new Error("Provide --username or --profile-url.");
  return `https://www.instagram.com/${args.username.replace(/^@/, "")}/`;
}

function normalizeInstagramMediaUrl(href) {
  const url = new URL(href);
  const parts = url.pathname.split("/").filter(Boolean);
  const markerIndex = parts.findIndex((part) => ["p", "reel", "tv"].includes(part));
  if (markerIndex < 0 || !parts[markerIndex + 1]) return null;
  return `https://www.instagram.com/${parts[markerIndex]}/${parts[markerIndex + 1]}/`;
}

async function collectUrls(page) {
  return page.evaluate(() => {
    return Array.from(document.querySelectorAll("a[href*='/p/'], a[href*='/reel/'], a[href*='/tv/']"))
      .map((anchor) => anchor.href)
      .filter(Boolean);
  });
}

async function discoverProfileUrls(args) {
  const browser = await chromium.launch({ headless: !args.headed });
  const page = await browser.newPage({
    viewport: { width: 1280, height: 900 },
    userAgent:
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
  });

  const discovered = new Map();
  try {
    await page.goto(profileUrl(args), { waitUntil: "domcontentloaded", timeout: 60_000 });
    await page.waitForTimeout(args.waitMs);

    for (let attempt = 0; attempt <= args.scrolls; attempt += 1) {
      for (const href of await collectUrls(page)) {
        const normalized = normalizeInstagramMediaUrl(href);
        if (normalized && !discovered.has(normalized)) {
          discovered.set(normalized, normalized);
        }
      }
      if (discovered.size >= args.limit) break;

      const showMore = page.getByRole("button", { name: /show more posts/i });
      if (await showMore.isVisible().catch(() => false)) {
        await showMore.click().catch(() => undefined);
      } else {
        await page.mouse.wheel(0, 2200);
      }
      await page.waitForTimeout(args.waitMs);
    }
  } finally {
    await browser.close();
  }

  return Array.from(discovered.keys()).slice(0, args.limit);
}

function writeOutput(filePath, urls) {
  const resolved = path.resolve(filePath);
  fs.mkdirSync(path.dirname(resolved), { recursive: true });
  fs.writeFileSync(resolved, `${urls.join("\n")}\n`, "utf8");
  return resolved;
}

function saveCandidates(args, urlFile) {
  const result = spawnSync(
    args.python,
    [
      "scripts/instagram_scrape_urls.py",
      "--file",
      urlFile,
      "--limit",
      String(args.limit),
      "--save-account",
      args.saveAccount,
      "--pretty",
    ],
    { stdio: "inherit", shell: process.platform === "win32" },
  );
  if (result.status !== 0) {
    throw new Error(`Metadata scrape failed with exit code ${result.status}`);
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) {
    console.log(usage());
    return;
  }
  if (!Number.isFinite(args.limit) || args.limit < 1) throw new Error("--limit must be >= 1.");
  if (!Number.isFinite(args.scrolls) || args.scrolls < 0) throw new Error("--scrolls must be >= 0.");

  const urls = await discoverProfileUrls(args);
  for (const url of urls) console.log(url);

  let outputPath = args.output;
  if (!outputPath && args.saveAccount) {
    const username = (args.username || new URL(profileUrl(args)).pathname.split("/").filter(Boolean)[0]).replace(
      /[^A-Za-z0-9_.-]/g,
      "_",
    );
    outputPath = path.join("data", "candidates", `instagram-${username}-urls.txt`);
  }
  if (outputPath) {
    const resolved = writeOutput(outputPath, urls);
    console.error(`Discovered ${urls.length} Instagram URLs -> ${resolved}`);
    if (args.saveAccount) saveCandidates(args, resolved);
  } else {
    console.error(`Discovered ${urls.length} Instagram URLs.`);
  }
}

main().catch((error) => {
  console.error(`FAILED: ${error.message}`);
  process.exitCode = 1;
});

// Job Radar relay (Cloudflare Worker): Telegram webhook -> GitHub workflow_dispatch, so commands are
// answered in about a minute instead of at the next scheduled run. Setup: README "Instant replies".
//
// Secrets (Worker > Settings > Variables and Secrets): TELEGRAM_SECRET, TELEGRAM_BOT_TOKEN,
// TELEGRAM_CHAT_ID, GITHUB_TOKEN (fine-grained, this repo only, Actions: read and write),
// GITHUB_REPO ("owner/job-radar").
//
// shortcut: GitHub keeps one pending run per concurrency group, so a command sent while a run is already
// queued can be dropped; the ack below tells you it was received, so resend if no reply in ~5 minutes.

export default {
  async fetch(request, env) {
    if (request.method !== "POST") return new Response("ok");
    // Telegram sends the secret we gave setWebhook; anything else is not Telegram
    if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.TELEGRAM_SECRET) {
      return new Response("forbidden", { status: 403 });
    }
    const update = await request.json().catch(() => ({}));
    const msg = update.message || {};
    const text = (msg.text || "").trim();
    if (String(msg.chat?.id) !== env.TELEGRAM_CHAT_ID || !text.startsWith("/")) {
      return new Response("ok"); // always 200, or Telegram retries the same update
    }
    const gh = await fetch(
      `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/run.yml/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GITHUB_TOKEN}`,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "job-radar-relay",
        },
        body: JSON.stringify({ ref: "main", inputs: { command: text.slice(0, 100) } }),
      },
    );
    const reply = gh.ok
      ? "⏳ Got it, reply in about a minute."
      : `⚠️ Couldn't start the run: GitHub answered ${gh.status}.` +
        (gh.status === 401 ? " The GitHub token has probably expired: make a new one and update GITHUB_TOKEN." : "");
    await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ chat_id: env.TELEGRAM_CHAT_ID, text: reply }),
    });
    return new Response("ok");
  },
};

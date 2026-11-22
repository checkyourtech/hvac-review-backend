/* Local previews only. Wix must supply verified-purchase proof through its
 * authenticated handoff; no credentials are put in URLs or browser storage.
 * The backend independently verifies this proof, ownership and the paid tier. */
(() => {
  "use strict";
  const form = document.getElementById("quote-upload");
  if (!form) return;
  const area = document.getElementById("quote-groups");
  const status = document.getElementById("upload-status");
  const submit = document.getElementById("submit-quotes");
  const add = document.getElementById("add-quote");
  const limit = Number(form.dataset.quoteLimit);
  const accepts = ".pdf,.docx,.txt,.jpg,.jpeg,.png,.heic,.heif";
  let groups = [[]], proof = null, busy = false, submissionKey = null;
  let session = 0, polling = 0;
  const objectURLs = new Set();
  const say = text => { status.textContent = text; };

  // Call after the real Wix/authenticated backend handoff is configured.
  window.configureQuoteUpload = credentials => {
    session++; polling++;
    document.getElementById("saved-reviews")?.remove();
    groups = [[]]; submissionKey = null; busy = false;
    releaseURLs();
    if (!credentials || typeof credentials.authorization !== "string" ||
        !credentials.authorization.startsWith("Bearer ") || typeof credentials.purchaseId !== "string") {
      proof = null;
      submit.disabled = true;
      render();
      say("Sign in to check your saved reviews.");
      return;
    }
    proof = { Authorization: credentials.authorization, "X-Purchase-ID": credentials.purchaseId };
    const current = session;
    submit.disabled = busy;
    say("Add every page, check the previews, then submit your quotes.");
    recoverReviews(true).catch(() => { if (current === session) say("Saved reviews could not be checked. Reconnect before resubmitting."); });
    render();
  };
  function releaseURLs() {
    for (const url of objectURLs) URL.revokeObjectURL(url);
    objectURLs.clear();
  }
  function button(text, action) {
    const element = document.createElement("button");
    element.type = "button";
    element.textContent = text;
    element.addEventListener("click", () => { submissionKey = null; action(); });
    element.disabled = busy;
    return element;
  }
  function picker(group, camera, replaceIndex = null) {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = camera ? "image/jpeg,image/png,image/heic,image/heif" : accepts;
    input.multiple = !camera && replaceIndex === null;
    if (camera) input.setAttribute("capture", "environment");
    input.setAttribute("aria-label", camera ? "Take a photo of a proposal page" : "Choose proposal pages or photos");
    input.addEventListener("change", () => {
      submissionKey = null;
      const selected = Array.from(input.files || []);
      if (!selected.length) return;
      if (group.length + selected.length - (replaceIndex === null ? 0 : 1) > 10) {
        say("Each quote supports up to 10 attached files or photos."); return;
      }
      if (selected.some(file => file.size === 0 || file.size > 10 * 1024 * 1024)) {
        say("Choose non-empty files of 10 MiB or less."); return;
      }
      const pages = selected.map(file => ({file, rotation: 0}));
      if (replaceIndex === null) group.push(...pages);
      else group.splice(replaceIndex, 1, ...pages);
      say("Check that every page is readable and in the correct quote group.");
      render();
    });
    return input;
  }
  function render() {
    releaseURLs();
    area.replaceChildren();
    groups.forEach((group, quoteIndex) => {
      const section = document.createElement("section");
      section.className = "quote-group";
      const heading = document.createElement("h3");
      heading.textContent = `Contractor quote ${quoteIndex + 1}`;
      section.append(heading);
      const gallery = picker(group, false);
      const camera = picker(group, true);
      gallery.disabled = camera.disabled = busy;
      const galleryLabel = document.createElement("label");
      galleryLabel.textContent = "Choose documents or gallery photos";
      galleryLabel.append(gallery);
      const cameraLabel = document.createElement("label");
      cameraLabel.textContent = "Take a photo";
      cameraLabel.append(camera);
      section.append(galleryLabel, cameraLabel);
      group.forEach((page, index) => {
        const row = document.createElement("div");
        row.className = "page-preview";
        const label = document.createElement("span");
        label.textContent = `${index + 1}. ${page.file.name}`;
        row.append(label);
        if (/\.(jpe?g|png|heic|heif)$/i.test(page.file.name)) {
          const image = document.createElement("img");
          const url = URL.createObjectURL(page.file);
          objectURLs.add(url);
          image.src = url;
          image.alt = `Preview of page ${index + 1}`;
          // Square layout keeps a rotated thumbnail inside its own space.
          image.style.width = "110px";
          image.style.height = "110px";
          image.style.transform = `rotate(${page.rotation}deg)`;
          image.onerror = () => { image.alt = "Preview unavailable in this browser; choose a JPG copy to preview."; };
          row.append(image, button("Rotate", () => { page.rotation = (page.rotation + 90) % 360; render(); }));
        }
        const replacement = picker(group, false, index);
        replacement.hidden = true;
        row.append(button("Replace", () => replacement.click()), replacement,
                   button("Remove", () => { group.splice(index, 1); render(); }));
        if (index) row.append(button("Move up", () => {
          [group[index - 1], group[index]] = [group[index], group[index - 1]]; render();
        }));
        section.append(row);
      });
      if (groups.length > 1) section.append(button("Remove quote", () => { groups.splice(quoteIndex, 1); render(); }));
      area.append(section);
    });
    add.hidden = groups.length >= limit;
    add.disabled = busy;
    submit.disabled = busy || !proof;
  }
  form.addEventListener("input", () => { submissionKey = null; });
  function showReport(report) {
    session++; polling++;
    releaseURLs();
    proof = null;
    document.open(); document.write(report); document.close();
  }
  async function securedFetch(url, options = {}, timeoutMs = 15000) {
    if (!proof) throw new Error("Sign in required");
    const current = session, controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(url, {...options, headers: {...proof, ...(options.headers || {})},
        cache: "no-store", signal: controller.signal});
      // Buffer before checking the session: a late response must never render
      // another signed-in customer's saved job, report, or error details.
      const text = await response.text();
      if (current !== session) throw new Error("Session changed");
      return {ok: response.ok, status: response.status, text: () => text, json: () => JSON.parse(text)};
    } finally { clearTimeout(timer); }
  }
  async function openSavedReport(jobId) {
    const current = session;
    const response = await securedFetch(`/jobs/${encodeURIComponent(jobId)}/report`);
    if (!response.ok) throw new Error("Report unavailable");
    const report = await response.text();
    if (current === session) showReport(report);
  }
  async function recoverReviews(autoFollow = false) {
    const current = session;
    const response = await securedFetch("/jobs");
    if (!response.ok) throw new Error("Recovery unavailable");
    const body = await response.json();
    if (current !== session) return;
    const prior = document.getElementById("saved-reviews");
    if (prior) prior.remove();
    const list = document.createElement("div");
    list.id = "saved-reviews";
    const refresh = document.createElement("button");
    refresh.type = "button";
    refresh.textContent = "Refresh saved reviews";
    refresh.onclick = () => recoverReviews().catch(() => say("Reconnect to check saved reviews before submitting again."));
    list.append(refresh);
    for (const job of body.jobs || []) {
      const row = document.createElement("p");
      row.textContent = `Review ${job.job_id}: ${job.state}. `;
      if (job.state === "completed") {
        const view = document.createElement("button");
        view.type = "button";
        view.textContent = "Open saved report";
        view.onclick = () => openSavedReport(job.job_id).catch(() => say("Sign in again to recover your report."));
        row.append(view);
      } else if (["queued", "pending", "processing"].includes(job.state)) {
        const watch = document.createElement("button");
        watch.type = "button";
        watch.textContent = "Follow review status";
        watch.onclick = () => waitForReview(job).catch(() => { if (current === session) say(`Reconnect to check review ${job.job_id}. Do not resubmit.`); });
        row.append(watch);
      }
      if (job.error) row.append(document.createTextNode(job.error.message));
      if (job.notification?.retry_allowed) {
        const retry = document.createElement("button");
        retry.type = "button";
        retry.textContent = "Retry service notification";
        retry.onclick = async () => {
          retry.disabled = true;
          try {
            const response = await securedFetch(`/jobs/${encodeURIComponent(job.job_id)}/delivery-retry`, {method: "POST"});
            if (!response.ok) throw new Error("Retry unavailable");
            await recoverReviews();
          } catch (_) { say("Notification could not be retried. Your saved report remains available."); }
        };
        row.append(retry);
      }
      list.append(row);
    }
    status.after(list);
    const pending = (body.jobs || []).find(job => ["queued", "pending", "processing"].includes(job.state));
    if (autoFollow && pending && current === session) {
      waitForReview(pending).catch(() => { if (current === session) say("Reconnect to check saved review status."); });
    }
  }
  async function waitForReview(job) {
    const watch = ++polling, current = session;
    for (let poll = 0; poll < 60; poll++) {
      if (watch !== polling || current !== session) return;
      if (job.state === "completed") { await openSavedReport(job.job_id); return; }
      if (job.state === "failed") {
        say(`Review ${job.job_id}: ${job.error?.message || "The review could not finish. Contact support with this review ID."}`);
        await recoverReviews();
        return;
      }
      const position = job.approximate_queue_position ? ` Approximate queue position: ${job.approximate_queue_position}.` : "";
      const paused = job.automatic_retry_pending ? " Purchase verification will be retried automatically." : "";
      say(`Submission saved. Review ${job.job_id} is ${job.state}.${position}${paused} You may close this page and sign in later. Processing times are not guaranteed.`);
      await new Promise(resolve => setTimeout(resolve, Math.min(15000, 5000 + poll * 500)));
      if (watch !== polling || current !== session) return;
      const response = await securedFetch(`/jobs/${encodeURIComponent(job.job_id)}`);
      if (!response.ok) throw new Error("Status unavailable");
      job = await response.json();
    }
    if (watch !== polling || current !== session) return;
    say(`Automatic refresh paused. Review ${job.job_id} remains saved. Use Refresh saved reviews or sign in later. Do not resubmit while it is queued or processing.`);
    await recoverReviews();
  }
  add.addEventListener("click", () => { if (!busy && groups.length < limit) { submissionKey = null; groups.push([]); render(); } });
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (busy || !proof) { say("Secure purchase verification is required before uploading."); return; }
    if (groups.some(group => !group.length)) { say("Add pages to each quote group or remove the empty group."); return; }
    if (groups.flat().reduce((total, page) => total + page.file.size, 0) > 30 * 1024 * 1024) {
      say("All attached files together must be 30 MiB or less."); return;
    }
    const data = new FormData();
    data.set("customer_name", form.elements.customer_name.value);
    data.set("customer_email", form.elements.customer_email.value);
    data.set("package", form.elements.package.value);
    let index = 0;
    data.set("quote_groups", JSON.stringify(groups.map(group => ({pages: group.map(page => ({file: index++, rotation: page.rotation}))}))));
    groups.flat().forEach(page => data.append("files", page.file, page.file.name));
    submissionKey ||= crypto.randomUUID();
    const current = session;
    busy = true; render(); say("Reading your pages and preparing your review. Completed reports can be recovered after signing in.");
    try {
      // Upload parsing/preparation has a longer existing server-side budget
      // than lightweight status requests. A lost response still recovers by ID.
      const response = await securedFetch("/upload", {method: "POST", headers: {"Idempotency-Key": submissionKey}, body: data}, 240000);
      if (current !== session) return;
      if (response.status === 202) { await waitForReview(response.json()); return; }
      if (!response.ok) {
        let body = {};
        try { body = response.json(); } catch (_) { /* Safe generic message below. */ }
        const detail = body.detail;
        const message = typeof detail === "string" ? detail : detail?.message || body.error?.message || "The upload could not finish. Check saved review status before retrying.";
        const location = typeof detail?.file === "number" ? `Attached file ${detail.file + 1}${detail.page ? `, page ${detail.page}` : ""}: ` : "";
        say(location + message);
        return;
      }
      const report = await response.text();
      if (current === session) showReport(report);
    } catch (_) {
      if (current === session) say("The connection was interrupted. Keep your selected pages and sign in again to check saved reviews. Resubmitting unchanged pages with this key will check the same job.");
    } finally {
      if (current === session) {
        busy = false;
        if (document.getElementById("quote-upload")) render();
      }
    }
  });
  window.addEventListener("pagehide", () => { session++; polling++; proof = null; releaseURLs(); });
  render();
})();

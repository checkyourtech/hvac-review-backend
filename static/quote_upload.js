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
  let groups = [[]], proof = null, busy = false;
  const objectURLs = new Set();
  const say = text => { status.textContent = text; };

  // Call after the real Wix/authenticated backend handoff is configured.
  window.configureQuoteUpload = credentials => {
    if (!credentials || typeof credentials.authorization !== "string" ||
        !credentials.authorization.startsWith("Bearer ") || typeof credentials.purchaseId !== "string") {
      proof = null;
      submit.disabled = true;
      return;
    }
    proof = { Authorization: credentials.authorization, "X-Purchase-ID": credentials.purchaseId };
    submit.disabled = busy;
    say("Add every page, check the previews, then submit your quotes.");
  };
  function releaseURLs() {
    for (const url of objectURLs) URL.revokeObjectURL(url);
    objectURLs.clear();
  }
  function button(text, action) {
    const element = document.createElement("button");
    element.type = "button";
    element.textContent = text;
    element.addEventListener("click", action);
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
  add.addEventListener("click", () => { if (!busy && groups.length < limit) { groups.push([]); render(); } });
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
    busy = true; render(); say("Reading your pages and preparing your review. Keep this page open.");
    try {
      const response = await fetch("/upload", {method: "POST", headers: proof, body: data});
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        const detail = body.detail;
        const message = typeof detail === "string" ? detail : detail?.message || "The upload could not finish. Please try again.";
        const location = typeof detail?.file === "number" ? `Attached file ${detail.file + 1}${detail.page ? `, page ${detail.page}` : ""}: ` : "";
        say(location + message);
        return;
      }
      const report = await response.text();
      releaseURLs();
      proof = null;
      document.open(); document.write(report); document.close();
    } catch (_) {
      say("The connection was interrupted. Keep your selected pages. If analysis had started, contact support before resubmitting.");
    } finally {
      busy = false;
      if (document.getElementById("quote-upload")) render();
    }
  });
  window.addEventListener("pagehide", releaseURLs);
  render();
})();

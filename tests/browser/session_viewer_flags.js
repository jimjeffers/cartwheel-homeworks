// With the viewer open in Playwright CLI, run:
// playwright-cli run-code --filename tests/browser/session_viewer_flags.js
// A separate browser context keeps the user's saved flags untouched.
async (page) => {
  const context = await page.context().browser().newContext();
  const name = "viewer-flags-regression.jsonl";
  const recording = [1, 2, 3].map(id => JSON.stringify({
    session_id: `flag-test-${id}`, role: "shopper",
    request: `Question ${id}`, response: `Answer ${id}`,
  })).join("\n");
  const load = async tab => {
    await tab.goto(page.url());
    await tab.getByRole("button", { name: "Open JSONL", exact: true }).waitFor();
    await tab.locator("#file-input").setInputFiles({
      name, mimeType: "application/x-ndjson", buffer: Buffer.from(recording),
    });
    await tab.getByText(name, { exact: true }).waitFor();
  };
  const toggle = async (tab, index, action) => {
    await tab.locator(`[data-session-index="${index}"]`).click({ button: "right" });
    await tab.getByRole("menuitem", { name: action, exact: true }).click();
  };
  const expectFlags = async (tab, expected) => {
    await tab.waitForFunction(indices => {
      const actual = [...document.querySelectorAll(".session-button")]
        .filter(button => button.querySelector(".problematic"))
        .map(button => Number(button.dataset.sessionIndex));
      return JSON.stringify(actual) === JSON.stringify(indices);
    }, expected, { timeout: 5000 });
  };
  try {
    const first = await context.newPage();
    const second = await context.newPage();
    await load(first);
    await load(second);
    await toggle(first, 0, "Flag as problematic");
    await toggle(second, 1, "Flag as problematic");
    for (const tab of [first, second]) {
      await expectFlags(tab, [0, 1]);
      await load(tab);
      await expectFlags(tab, [0, 1]);
    }
    await toggle(first, 0, "Remove problematic flag");
    await toggle(second, 2, "Flag as problematic");
    for (const tab of [first, second]) {
      await expectFlags(tab, [1, 2]);
      await load(tab);
      await expectFlags(tab, [1, 2]);
    }
    await second.getByRole("combobox", { name: "Show", exact: true }).selectOption("problematic");
    await toggle(first, 1, "Remove problematic flag");
    await expectFlags(second, [2]);
    await first.evaluate(() => localStorage.clear());
    await expectFlags(second, []);

    const legacyKey = `cartwheel.session-viewer.problematic.v1:${name}`;
    const legacyValue = JSON.stringify([JSON.stringify(["id", "flag-test-1"])]);
    await first.evaluate(({ key, value }) => localStorage.setItem(key, value), {
      key: legacyKey, value: legacyValue,
    });
    await load(first);
    await expectFlags(first, [0]);
    await toggle(first, 0, "Remove problematic flag");
    await load(first);
    await expectFlags(first, []);
    const preserved = await first.evaluate(key => localStorage.getItem(key), legacyKey);
    if (preserved !== legacyValue) throw new Error("Legacy storage was modified");

    await first.evaluate(() => {
      Storage.prototype.setItem = () => { throw new DOMException("Full", "QuotaExceededError"); };
    });
    await toggle(first, 1, "Flag as problematic");
    await expectFlags(first, [1]);
    await first.getByText(
      "Flag updated for this visit, but could not be saved. Browser storage may be unavailable or full.",
      { exact: true },
    ).waitFor();
    return "PASS: two-tab add/remove, synchronization, reload, filtering, legacy flags, and storage failure";
  } finally {
    await context.close();
  }
}

const form = document.querySelector("#analysis-form");
const button = document.querySelector("#submit-button");
const status = document.querySelector("#status");
const results = document.querySelector("#results");

const text = (id, value) => { document.querySelector(id).textContent = value ?? "—"; };
const showStatus = (message, kind) => {
  status.hidden = false;
  status.className = `status ${kind || ""}`;
  status.textContent = message;
};

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  button.disabled = true;
  button.querySelector("span").textContent = "Analyzing real data…";
  results.hidden = true;
  showStatus("Collecting Git and file evidence, then requesting a validated explanation…", "loading");
  const data = Object.fromEntries(new FormData(form).entries());
  data.no_diff = form.no_diff.checked;

  try {
    const response = await fetch("/api/analyze", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(data),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Analysis failed.");
    render(payload);
    status.hidden = true;
  } catch (error) {
    showStatus(error.message, "error");
  } finally {
    button.disabled = false;
    button.querySelector("span").textContent = "Run analysis";
  }
});

function render(payload) {
  const d = payload.deterministic;
  const r = payload.final_rca;
  text("#incident-id", d.analysis_id);
  text("#environment-badge", d.environment.toUpperCase());
  text("#root-cause", d.root_cause);
  text("#confidence-value", d.confidence == null ? "—" : `${Math.round(d.confidence * 100)}%`);
  text("#severity", d.severity ? d.severity.toUpperCase() : "—");
  text("#commit-sha", d.commit_sha);
  text("#repository", d.repository);

  const evidence = document.querySelector("#evidence-list");
  evidence.replaceChildren();
  (d.evidence || []).forEach((item) => {
    const row = document.createElement("div");
    row.className = "evidence";
    const marker = document.createElement("span");
    marker.className = "evidence-marker";
    marker.textContent = "✓";
    const copy = document.createElement("div");
    const source = document.createElement("strong");
    source.textContent = item.source || "Evidence";
    const reference = document.createElement("small");
    reference.textContent = item.raw_reference || item.id;
    copy.append(source, reference);
    row.append(marker, copy);
    evidence.append(row);
  });

  const explanation = r?.explanation;
  const fix = r?.fix;
  text("#explanation", explanation?.summary);
  text("#reasoning", explanation?.reasoning);
  text("#fix-description", fix?.description);
  const steps = document.querySelector("#fix-steps");
  steps.replaceChildren();
  (fix?.steps || []).forEach((step) => {
    const item = document.createElement("li");
    item.textContent = step;
    steps.append(item);
  });
  results.hidden = false;
  results.scrollIntoView({behavior: "smooth", block: "start"});
}
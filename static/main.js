// static/main.js
let previewJobId = null;
let previewData = [];

document.getElementById("uploadBtn").addEventListener("click", async () => {
  const fileInput = document.getElementById("fileInput");
  if (!fileInput.files.length) { alert("Pick an Excel file first"); return; }
  const file = fileInput.files[0];
  const fd = new FormData();
  fd.append("file", file);
  const res = await fetch("/upload", { method: "POST", body: fd });
  const j = await res.json();
  if (!res.ok) {
    alert("Upload error: " + (j.error || "unknown"));
    return;
  }
  previewJobId = j.job_id;
  // fetch preview
  const prev = await (await fetch(`/preview/${previewJobId}`)).json();
  previewData = prev.preview || [];
  populatePreviewTable(previewData);
});

function populatePreviewTable(data) {
  document.getElementById("previewArea").style.display = "block";
  const tbody = document.querySelector("#previewTable tbody");
  tbody.innerHTML = "";
  data.forEach((r, idx) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td><input type="checkbox" data-index="${idx}" /></td>
                    <td>${escapeHtml(r.fullname)}</td>
                    <td>${escapeHtml(r.phone)}</td>`;
    tbody.appendChild(tr);
  });
}

function escapeHtml(s){ return (s||"").toString().replace(/&/g,'&amp;').replace(/</g,'&lt;') }

document.getElementById("selectAllBtn").addEventListener("click", ()=> {
  document.querySelectorAll("#previewTable tbody input[type=checkbox]").forEach(cb=>cb.checked=true);
});
document.getElementById("deselectAllBtn").addEventListener("click", ()=> {
  document.querySelectorAll("#previewTable tbody input[type=checkbox]").forEach(cb=>cb.checked=false);
});

async function gatherTargetsFromSelection(all=false){
  if (all) return previewData;
  const boxes = Array.from(document.querySelectorAll("#previewTable tbody input[type=checkbox]"));
  const targets = boxes.map((b,i)=>{
    if (b.checked) return previewData[i];
    return null;
  }).filter(Boolean);
  return targets;
}

async function startSend(targets, personalize){
  const message = document.getElementById("messageBox").value;
  if (!message) { alert("Please write a message"); return; }
  // POST /send
  const body = {
    message,
    personalize,
    targets
  };
  const res = await fetch("/send", {
    method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify(body)
  });
  const j = await res.json();
  if (!res.ok) { alert("Send error: " + (j.error || "unknown")); return; }
  const send_job_id = j.send_job_id;
  document.getElementById("progressContainer").style.display = "block";
  pollProgress(send_job_id);
}

async function pollProgress(jobId){
  const fill = document.getElementById("progressFill");
  const text = document.getElementById("progressText");
  const interval = setInterval(async ()=>{
    const res = await fetch(`/progress/${jobId}`);
    const j = await res.json();
    if (!res.ok) { text.textContent = "Error fetching progress"; clearInterval(interval); return; }
    fill.style.width = j.percent + "%";
    fill.textContent = j.percent + "%";
    text.textContent = `Status: ${j.status} — Sent: ${j.sent} / ${j.total} — Failed: ${j.failed}`;
    if (j.status === "completed" || j.status === "failed") {
      clearInterval(interval);
    }
  }, 1000);
}

document.getElementById("sendSelectedBtn").addEventListener("click", async ()=>{
  const targets = await gatherTargetsFromSelection(false);
  if (!targets.length) { if(!confirm("No recipients selected. Send to all instead?")) return; }
  const personalize = document.getElementById("personalizeToggle").checked;
  await startSend(targets.length ? targets : previewData, personalize);
});

document.getElementById("sendAllBtn").addEventListener("click", async ()=>{
  const personalize = document.getElementById("personalizeToggle").checked;
  await startSend(previewData, personalize);
});

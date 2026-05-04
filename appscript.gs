// Velocity Shipping — QA Report receiver
// Paste this entire file into: Extensions → Apps Script → replace all content → Save → Deploy

const SECRET = "change-this-to-a-random-string"; // must match GOOGLE_APPS_SCRIPT_SECRET in .env

const CALL_HEADERS = [
  "Date", "Call ID", "Call Type", "Agent Name", "Agent Key",
  "Status", "Duration (s)", "Recording URL", "Transcript Preview",
  "Overall Quality", "Overall Score (/10)", "No Contact",
  "Opening Score", "Pacing Score", "Interruption Score", "Language Score",
  "Script Score", "Objection Score", "Closure Score", "Effectiveness Score",
  "Opening Observation", "Pacing Observation", "Interruption Observation",
  "Language Observation", "Script Observation", "Objection Observation",
  "Closure Observation", "Effectiveness Observation",
  "Opening Fix", "Pacing Fix", "Interruption Fix", "Language Fix",
  "Script Fix", "Objection Fix", "Closure Fix", "Effectiveness Fix",
  "Top Issues", "Key Recommendation"
];

const SUMMARY_HEADERS = [
  "Date", "Total Calls", "No Contact", "Contactable",
  "Good", "Average", "Poor",
  "Avg Opening", "Avg Pacing", "Avg Interruption", "Avg Language",
  "Avg Script", "Avg Objection", "Avg Closure", "Avg Effectiveness",
  "Executive Summary"
];

function doPost(e) {
  try {
    var payload = JSON.parse(e.postData.contents);

    if (payload.secret !== SECRET) {
      return respond({ status: "error", message: "Unauthorized" });
    }

    var ss = SpreadsheetApp.getActiveSpreadsheet();

    if (payload.type === "calls") {
      var ws = getOrCreateSheet(ss, "Call Analysis", CALL_HEADERS);
      var rows = payload.rows || [];
      if (rows.length > 0) {
        ws.getRange(ws.getLastRow() + 1, 1, rows.length, rows[0].length).setValues(rows);
      }
      return respond({ status: "ok", rows_written: rows.length });
    }

    if (payload.type === "summary") {
      var ws2 = getOrCreateSheet(ss, "Daily Summary", SUMMARY_HEADERS);
      var row = payload.row || [];
      if (row.length > 0) {
        ws2.appendRow(row);
      }
      return respond({ status: "ok" });
    }

    return respond({ status: "error", message: "Unknown type: " + payload.type });

  } catch (err) {
    return respond({ status: "error", message: err.toString() });
  }
}

function getOrCreateSheet(ss, name, headers) {
  var ws = ss.getSheetByName(name);
  if (!ws) {
    ws = ss.insertSheet(name);
    ws.appendRow(headers);
    var headerRange = ws.getRange(1, 1, 1, headers.length);
    headerRange.setFontWeight("bold")
               .setBackground("#1a73e8")
               .setFontColor("#ffffff");
    ws.setFrozenRows(1);
  }
  return ws;
}

function respond(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

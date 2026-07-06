/**
 * Sumio — Google Sheets add-on (the "different Hands").
 *
 * Same Brain: this script never re-implements any operation. It reads the live sheet,
 * asks the Sumio backend for a plan (/sheets/plan) and the transformed grid (/sheets/apply),
 * then writes the result back in place. All the intelligence + governance (PII shield,
 * guardrails, personalization) lives in the backend.
 *
 * Safety handled here:
 *   • Permission-aware — view-only users get suggestions; we never attempt a write for them.
 *   • Edit sequencing — applies run inside a document lock and re-read the sheet, so an AI
 *     edit can't silently overwrite a human edit made mid-flight (the backend also rejects a
 *     stale base_hash).
 *
 * Setup: Extensions → Apps Script, paste these files, then set a Script Property
 * `SUMIO_API_URL` to your deployed backend's base URL (must be HTTPS reachable by Google).
 */

function _apiUrl_() {
  return (
    PropertiesService.getScriptProperties().getProperty('SUMIO_API_URL') ||
    'http://localhost:8000'
  );
}

/** Add-on menu + sidebar. */
function onOpen() {
  SpreadsheetApp.getUi().createMenu('Sumio').addItem('Open Sumio', 'showSidebar').addToUi();
}
function onInstall() {
  onOpen();
}
function showSidebar() {
  var html = HtmlService.createHtmlOutputFromFile('Sidebar').setTitle('Sumio');
  SpreadsheetApp.getUi().showSidebar(html);
}

/** Read the active sheet's grid + whether the current user may edit it. */
function _context_() {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
  return {
    sheetName: sheet.getName(),
    values: sheet.getDataRange().getValues(),
    canEdit: _canEdit_(),
  };
}

/**
 * Probe edit access: a no-op write succeeds for editors and throws for view-only users.
 * (There is no first-class "is the current user a viewer" API.)
 */
function _canEdit_() {
  try {
    var r = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet().getRange(1, 1);
    r.setValue(r.getValue());
    SpreadsheetApp.flush();
    return true;
  } catch (e) {
    return false;
  }
}

function _fetchJson_(path, payload) {
  // Optional API key (Script Property SUMIO_API_TOKEN). Genuinely secret here — it lives
  // server-side in the add-on, not in any browser bundle.
  var token = PropertiesService.getScriptProperties().getProperty('SUMIO_API_TOKEN');
  var headers = token ? { 'X-API-Key': token } : {};
  var res = UrlFetchApp.fetch(_apiUrl_() + path, {
    method: 'post',
    contentType: 'application/json',
    headers: headers,
    payload: JSON.stringify(payload),
    muteHttpExceptions: true,
  });
  var text = res.getContentText();
  try {
    return JSON.parse(text);
  } catch (e) {
    return { status: 'error', error: 'The backend returned an unreadable response.' };
  }
}

/**
 * Called by the sidebar. Returns a PLAN (editors) or a SUGGESTION (view-only users) —
 * the latter is read-only by design.
 */
function planRequest(instruction) {
  var ctx = _context_();
  var resp = _fetchJson_('/sheets/plan', {
    instruction: instruction,
    values: ctx.values,
    sheet_name: ctx.sheetName,
    can_edit: ctx.canEdit,
  });
  resp.can_edit = ctx.canEdit;
  return resp;
}

/**
 * Called by the sidebar to APPLY a plan. Sequenced with a document lock + a fresh re-read,
 * so concurrent human edits aren't clobbered. Returns the backend's response; on "applied"
 * the new grid has already been written to the sheet.
 */
function applyRequest(plan, baseHash, confirm) {
  if (!_canEdit_()) {
    return {
      status: 'error',
      error: "You have view-only access — Sumio can suggest changes but can't edit this sheet.",
    };
  }
  var lock = LockService.getDocumentLock();
  try {
    lock.waitLock(20000);
  } catch (e) {
    return { status: 'error', error: 'The sheet is busy with another change — try again in a moment.' };
  }
  try {
    var sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
    var current = sheet.getDataRange().getValues(); // re-read inside the lock
    var resp = _fetchJson_('/sheets/apply', {
      plan: plan,
      values: current,
      base_hash: baseHash,
      sheet_name: sheet.getName(),
      confirm: !!confirm,
    });
    if (resp.status === 'applied') {
      _writeValues_(sheet, resp.values);
      SpreadsheetApp.flush();
    }
    return resp;
  } finally {
    lock.releaseLock();
  }
}

/** Overwrite the sheet with a new grid (clears first; normalizes ragged rows). */
function _writeValues_(sheet, values) {
  sheet.clearContents();
  if (!values || !values.length) return;
  var width = values[0].length;
  var norm = values.map(function (row) {
    row = row.slice(0, width);
    while (row.length < width) row.push('');
    return row;
  });
  sheet.getRange(1, 1, norm.length, width).setValues(norm);
}

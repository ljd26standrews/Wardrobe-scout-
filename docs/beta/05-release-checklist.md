# Private beta release checklist

This branch is a reviewable private-beta candidate, not a claim that every recommendation will suit its user.
The remaining personal-taste validation is the [pilot](03-pilot.md). Keep the PR in draft until the required
platform checks are green and the Windows launch smoke check is recorded.

## Automated checks and evidence

- [x] Backend suite passed locally after the Windows fixes: 436 tests (Python 3.12.14, Linux).
- [x] Cache/model follow-up: 45 focused tests passed; exact 3,000-listing before/after fingerprints match.
- [x] Production frontend build, TypeScript, lint and light/dark token contrast checks passed.
- [x] Full 1,000/5,000/20,000-listing benchmark and raw results recorded in [04-performance.md](04-performance.md).
- [x] Populated exposure migration preserves legacy events through upgrade, downgrade and upgrade again.
- [x] Full-snapshot restoration preserves grades, decisions, frozen benchmark hashes, read-only records and run
  history; the restored live database receives a new cache identity and can migrate without changing frozen data.
- [ ] Latest GitHub Linux backend/frontend, Windows backend, Chromium and WebKit jobs all green.
- [ ] Windows launcher/browser smoke check below completed on a real Windows installation.
- [ ] Personal pilot collected and reported. Synthetic test results do not satisfy this item.

The first Windows CI run exposed canonical-newline and console-encoding bugs, plus POSIX-only assumptions in
three tests. They were fixed rather than excluded from the Windows suite. The first all-browser run reused a
mutable catalogue across browser engines; CI now provisions a separate synthetic installation for each engine.

The browser suites cover desktop and mobile flows, rapid decisions, Undo, failed requests, actual exposure
recording, navigation, horizontal reflow at 390/768/1440 pixels, a 320-pixel viewport, keyboard focus/names,
reduced motion and dark mode. These are automated checks, not a complete accessibility certification or a
substitute for testing with a screen reader. WebKit's iPhone profile is browser emulation, not a physical iPhone.

Local browser downloads/dependency installation failed in the execution environment. Do not describe local
Chromium/WebKit tests as passed; use the GitHub run results.

## Upgrade and rollback rehearsal

1. Record the current commit, data directory and any explicit `WS_DATABASE_URL`. Stop scans and finish evaluation
   commands. Keep the existing installation until the restored copy has been checked.
2. From `backend/`, use the installed interpreter to make a full snapshot:

   ```powershell
   .\.venv\Scripts\python.exe -m app.cli backup-full C:\Backups\wardrobe-before-beta.zip
   ```

   The snapshot includes SQLite, images/uploads, frozen benchmark records and snapshots, evaluation run history,
   and pilot reports. JSON export is a mergeable personal-data backup; it is not an exact installation restore.
   The live SQLite copy is transactional; separate evaluation files are not captured in the same transaction.
3. Restore to a new empty directory and verify a known benchmark:

   ```powershell
   .\.venv\Scripts\python.exe -m app.cli restore-full C:\Backups\wardrobe-before-beta.zip --data-dir C:\WardrobeBetaData
   $env:WS_DATA_DIR = 'C:\WardrobeBetaData'
   $env:WS_DATABASE_URL = 'sqlite:///C:/WardrobeBetaData/wardrobe_scout.db'
   .\.venv\Scripts\python.exe -m app.cli init-db
   .\.venv\Scripts\python.exe -m eval.benchmark verify pilot@v1
   ```

   Substitute an existing benchmark name; if none has been frozen, omit the last command. Explicitly set the
   restored database URL so an old `.env` setting cannot redirect the rehearsal to the original database.
4. Install the branch's dependencies with `-c constraints.txt`, run `npm ci` and `npm run build` in `frontend`, then
   run `scripts/start-windows.ps1` from the repo root. If `py` is unavailable, use the installed Python executable
   (`python --version` first) to create `.venv`. Check Python and Node are installed before troubleshooting the app.
5. Open `http://localhost:3000`. Check Discover and Learning picks, a product detail, save/Later, dislike and Undo,
   Saved notes, Evaluate, and a small seller scan. Close and restart the app: the same decisions and notes must
   remain. Test keyboard-only navigation and an ordinary mobile-sized viewport.
6. For rollback, stop both processes, select the previously recorded code revision and matching dependencies,
   and restore the pre-upgrade snapshot into another empty folder. Point both data settings at that folder.
   Do not run older code against the upgraded live database or overwrite the only backup. Decisions made after
   the backup are not in it; retain the newer data folder if those decisions need recovery.

## Pilot and release decision

Start with 150–300 readable listings from several sellers. Grade the independent Evaluate sample before viewing
results, freeze it, then collect normal Discover and Learning-pick decisions. Follow [03-pilot.md](03-pilot.md)
for the separate denominators and uncertainty bounds. Expanding to all sellers before this check increases scan
cost and makes it harder to diagnose whether failures come from parsing, evidence, images or preference learning.

A private-beta release requires passing platform checks, a successful restore/launch rehearsal, and a clear note
that continuous scans at 20,000 listings remain slower than ordinary decisions. High-confidence taste claims
require the user's held-out results; no synthetic benchmark can authorize those claims.

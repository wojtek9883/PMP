#!/usr/bin/env python3
"""
PMP Answer Fixer
================
Uzupełnia brakujące odpowiedzi w PMP_Baza_Pytan_1800.html używając Claude API.

Użycie:
    python3 fix_pmp_answers.py                          # nadpisuje oryginalny plik
    python3 fix_pmp_answers.py --out wynik.html         # zapisuje do nowego pliku
    python3 fix_pmp_answers.py --key sk-ant-XXXX        # klucz API jako argument
    python3 fix_pmp_answers.py --batch 10               # rozmiar batcha (domyślnie 5)
    python3 fix_pmp_answers.py --resume progress.json   # wznów po przerwie

Klucz API można też ustawić jako zmienną środowiskową:
    export ANTHROPIC_API_KEY=sk-ant-...
    python3 fix_pmp_answers.py
"""

import sys
import os
import re
import json
import gzip
import base64
import time
import argparse
import urllib.request
import urllib.error
from pathlib import Path

# ── Kolory w terminalu ────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
BLUE   = "\033[94m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def ok(msg):    print(f"{GREEN}✓{RESET} {msg}")
def err(msg):   print(f"{RED}✗{RESET} {msg}", file=sys.stderr)
def info(msg):  print(f"{BLUE}ℹ{RESET} {msg}")
def warn(msg):  print(f"{YELLOW}⚠{RESET} {msg}")
def bold(msg):  print(f"{BOLD}{msg}{RESET}")

# ── Parsowanie argumentów ─────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(description="PMP Answer Fixer — uzupełnia odpowiedzi przez Claude API")
    p.add_argument("input", nargs="?", default="PMP_Baza_Pytan_1800.html",
                   help="Plik HTML wejściowy (domyślnie: PMP_Baza_Pytan_1800.html)")
    p.add_argument("--out", "-o", default=None,
                   help="Plik wyjściowy (domyślnie: nadpisuje plik wejściowy)")
    p.add_argument("--key", "-k", default=None,
                   help="Klucz API Anthropic (sk-ant-...)")
    p.add_argument("--batch", "-b", type=int, default=5,
                   help="Liczba pytań w jednym batchu (domyślnie: 5)")
    p.add_argument("--resume", "-r", default="pmp_progress.json",
                   help="Plik z postępem do wznowienia (domyślnie: pmp_progress.json)")
    p.add_argument("--model", default="claude-sonnet-4-20250514",
                   help="Model Claude (domyślnie: claude-sonnet-4-20250514)")
    p.add_argument("--delay", type=float, default=0.5,
                   help="Opóźnienie między batchami w sekundach (domyślnie: 0.5)")
    return p.parse_args()

# ── Dekompresja danych z HTML ─────────────────────────────────────────────────
def load_questions(html_path):
    with open(html_path, encoding="utf-8") as f:
        html = f.read()

    match = re.search(r'COMPRESSED_DATA\s*=\s*"([^"]+)"', html)
    if not match:
        raise ValueError("Nie znaleziono COMPRESSED_DATA w pliku HTML")

    b64 = match.group(1)
    data = gzip.decompress(base64.b64decode(b64))
    questions = json.loads(data.decode("utf-8"))
    return questions, html, b64

# ── Kompresja i zapis do HTML ─────────────────────────────────────────────────
def save_questions(questions, original_html, out_path):
    json_bytes = json.dumps(questions, ensure_ascii=False).encode("utf-8")
    compressed = gzip.compress(json_bytes)
    new_b64 = base64.b64encode(compressed).decode("ascii")

    new_html = re.sub(
        r'(COMPRESSED_DATA\s*=\s*")[^"]*(")',
        r'\g<1>' + new_b64 + r'\g<2>',
        original_html
    )

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(new_html)

    return len(new_html)

# ── Claude API call ───────────────────────────────────────────────────────────
def call_claude(api_key, model, batch):
    prompt_parts = []
    for i, q in enumerate(batch):
        opts = "\n".join(f"{k}) {v}" for k, v in q["o"].items())
        hint = f"\nExplanation hint: {q['e'][:400]}" if q.get("e") else ""
        prompt_parts.append(
            f"Q{i+1} [ID:T{q['t']}-Q{q['n']}]: {q['q']}\n{opts}{hint}"
        )
    prompt = "\n\n---\n\n".join(prompt_parts)

    system = (
        "You are a PMP certification exam expert. "
        "For each question, determine the correct answer(s) based on the question text, options, and explanation hint.\n\n"
        "RULES:\n"
        "- Return ONLY a JSON array — no explanation, no markdown, no extra text\n"
        "- One entry per question, in the exact same order as input\n"
        '- Each entry: {"id": "T[test]-Q[num]", "answer": "X"}\n'
        "- For single-answer questions: one letter, e.g. \"B\"\n"
        "- For multi-answer questions (Choose two/three): comma-separated sorted letters, e.g. \"A,C\" or \"B,C,D\"\n"
        "- Use ONLY letters that appear in the options (A/B/C/D)\n"
        "- Base your answer on PMP best practices and the explanation hint if available"
    )

    payload = json.dumps({
        "model": model,
        "max_tokens": 2048,
        "system": system,
        "messages": [{"role": "user", "content": prompt}]
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
    )

    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read().decode("utf-8"))

    text = "".join(block.get("text", "") for block in body.get("content", []))
    clean = re.sub(r"```json|```", "", text).strip()
    return json.loads(clean)

# ── Pasek postępu ─────────────────────────────────────────────────────────────
def progress_bar(done, total, width=40):
    pct = done / total if total else 0
    filled = int(width * pct)
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {done}/{total} ({pct*100:.1f}%)"

# ── Główna logika ─────────────────────────────────────────────────────────────
def main():
    args = parse_args()

    bold("\n🤖 PMP Answer Fixer")
    print("=" * 50)

    # Klucz API
    api_key = args.key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        api_key = input(f"{CYAN}Podaj klucz API Anthropic (sk-ant-...): {RESET}").strip()
    if not api_key.startswith("sk-ant-"):
        err("Niepoprawny klucz API. Powinien zaczynać się od 'sk-ant-'")
        sys.exit(1)

    # Wczytaj plik
    html_path = Path(args.input)
    if not html_path.exists():
        err(f"Nie znaleziono pliku: {html_path}")
        sys.exit(1)

    out_path = Path(args.out) if args.out else html_path
    info(f"Plik wejściowy:  {html_path}")
    info(f"Plik wyjściowy:  {out_path}")
    info(f"Model:           {args.model}")
    info(f"Batch size:      {args.batch}")
    print()

    info("Wczytywanie pytań...")
    questions, original_html, _ = load_questions(html_path)
    null_q = [q for q in questions if q.get("a") is None and q.get("q") and q.get("o")]
    info(f"Łącznie pytań:   {len(questions)}")
    info(f"Bez odpowiedzi:  {len(null_q)}")
    print()

    # Wznowienie z pliku progress
    progress_path = Path(args.resume)
    answers = {}
    if progress_path.exists():
        with open(progress_path) as f:
            answers = json.load(f)
        warn(f"Wznowienie — wczytano {len(answers)} zapisanych odpowiedzi z {progress_path}")

    # Filtruj tylko te bez odpowiedzi i bez zapisanego postępu
    to_process = [
        q for q in null_q
        if f"T{q['t']}-Q{q['n']}" not in answers
    ]
    info(f"Pozostało do przetworzenia: {len(to_process)}")
    print()

    if not to_process:
        ok("Wszystkie odpowiedzi już uzupełnione!")
    else:
        total = len(to_process)
        done = 0
        errors = 0
        start = time.time()

        for i in range(0, total, args.batch):
            batch = to_process[i:i + args.batch]
            batch_num = i // args.batch + 1
            batch_total = (total + args.batch - 1) // args.batch

            # ETA
            if done > 0:
                elapsed = time.time() - start
                rate = done / elapsed
                remaining = (total - done) / rate
                eta = f"ETA {remaining/60:.1f}min"
            else:
                eta = "ETA ..."

            print(f"\r{progress_bar(done, total)}  Batch {batch_num}/{batch_total}  {eta}  ", end="", flush=True)

            try:
                results = call_claude(api_key, args.model, batch)

                for r in results:
                    qid = r.get("id", "")
                    ans = r.get("answer", "")
                    if qid and ans:
                        answers[qid] = ans
                        done += 1

                # Aplikuj i zapisz HTML po kazdym batchu
                # Postep jest w samym pliku HTML - przy kolejnym runie
                # skrypt widzi juz uzupelnione odpowiedzi i pomija je
                for q in questions:
                    key = f"T{q['t']}-Q{q['n']}"
                    if q.get("a") is None and key in answers:
                        q["a"] = answers[key]
                save_questions(questions, original_html, out_path)

                time.sleep(args.delay)

            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")
                errors += len(batch)
                print()
                err(f"Batch {batch_num} HTTP {e.code}: {body[:200]}")
                if e.code == 429:
                    warn("Rate limit — czekam 30 sekund...")
                    time.sleep(30)
                elif e.code in (401, 403):
                    err("Błąd autoryzacji — sprawdź klucz API")
                    sys.exit(1)
                else:
                    time.sleep(5)

            except Exception as e:
                errors += len(batch)
                print()
                err(f"Batch {batch_num}: {e}")
                time.sleep(3)

        print(f"\r{progress_bar(done + errors, total)}  Gotowe!                              ")
        print()
        ok(f"Uzupełniono {done} odpowiedzi, błędów: {errors}")

    # Policz ile uzupelniono w tej sesji
    fixed_count = sum(1 for q in questions if q.get("a") is not None and 
                      f"T{q['t']}-Q{q['n']}" in answers)
    info(f"Uzupelniono w tej sesji: {fixed_count} odpowiedzi")

    # Zapisz finalny HTML
    size = save_questions(questions, original_html, out_path)
    print()
    ok(f"Zapisano: {out_path}  ({size/1024:.0f} KB)")

    remaining_null = sum(1 for q in questions if q.get("a") is None and q.get("q"))
    if remaining_null > 0:
        warn(f"Pozostalo {remaining_null} pytan bez odpowiedzi")
        warn("Uruchom workflow ponownie - skrypt uzupelni brakujace")

    print()
    bold("✅ Gotowe! Możesz teraz wgrać plik do repo i zrobić deploy.")
    print()


if __name__ == "__main__":
    main()

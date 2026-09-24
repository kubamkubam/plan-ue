# Plan zajęć UEW (N1 · ZR/2)

Czytelna wersja planu z [plan.ue.wroc.pl](https://plan.ue.wroc.pl/), która aktualizuje się sama.

- `scraper/scrape.py` – co 2 godziny (GitHub Actions) pobiera plan grupy, lektorat i przedmioty do wyboru,
  wykrywa zmiany i zapisuje `docs/data.json` oraz kalendarz `docs/plan.ics`.
- `docs/index.html` – strona (działa na telefonie i komputerze, także offline).
- `config.json` – co pobierać: semestr, grupa, lektorat, przedmioty do wyboru.

## Typowe zmiany w `config.json`

- **Zapisałeś się na przedmiot społeczny** – usuń `"PS"` z `not_enrolled` i dopisz go do `electives`
  (kod `PS`, dzień tygodnia: 5 = sobota, 6 = niedziela, nazwa, prowadzący, `teacher_id` z listy
  „Pracownicy” na plan.ue.wroc.pl).
- **Nowy semestr** – zmień `semester_id` i `group` (widać je w adresie linku na plan.ue.wroc.pl,
  np. `l_pozycjaplanu1.php?se=60&gr=200/2`).

Po zapisaniu zmiany na GitHubie automat uruchamia się od razu. Ręcznie: zakładka **Actions → Aktualizacja planu → Run workflow**.

Lokalnie: `python scraper/scrape.py` (Python 3.9+, bez dodatkowych bibliotek).

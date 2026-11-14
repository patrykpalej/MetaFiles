# MetaFiles

Menedżer plików, który wygląda i działa jak Nautilus (Pliki GNOME), ale porządkuje pracę w projekty tak jak MetaTerminal. Projekty i grupy siedzą w prawym sidebarze, a w każdym projekcie otwierasz dowolnie wiele kart. Do tego funkcje, których Nautilus nie ma: podział widoku, podgląd plików, terminal w panelu, integracja z Gitem, szybkie otwieranie i własne akcje.

## Instalacja

```bash
./install.sh            # dodaje ~/.local/bin/metafiles i ikonę w menu GNOME
./install.sh --uninstall
```

Wymagane pakiety (w Ubuntu zwykle już są): `python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`.

Opcjonalne pakiety (instalator wypisze gotowe polecenie dla brakujących):

| Pakiet | Funkcja |
|---|---|
| `gir1.2-vte-3.91` | terminal w panelu (F4) |
| `gir1.2-gtksource-5` | kolorowanie składni w podglądzie |
| `gir1.2-poppler-0.18` | przewijany podgląd PDF |
| `meld` | porównywanie plików i zmian z Gita |
| `git` | status Gita, gałąź, pliki ignorowane |

## Użycie

```bash
metafiles                 # otwiera poprzednią sesję
metafiles ~/Dev/projekt   # dodaje katalog jako projekt (albo przełącza na istniejący)
```

### Projekty (jak w MetaTerminalu)

- **Projekty**: przycisk „Dodaj projekt” na górze prawego sidebara, Ctrl+N albo „Otwórz jako projekt” (Shift+Enter) na katalogu. Prawy klik na projekcie daje:
  - zmianę nazwy,
  - blokadę nawigacji,
  - otwarcie w MetaTerminalu lub Nautilusie,
  - grupę,
  - przesuwanie,
  - zamknięcie projektu (katalog na dysku zostaje nietknięty).
- **Nieprzypisane karty**: pierwszy wpis w sidebarze, do zwykłego przeglądania bez projektu (start w `~`, bez blokady).
- **Grupy**: przycisk „Nowa grupa”. Przycisk „+” w nagłówku grupy dodaje projekt od razu do niej. Projekty przeciąga się myszą między grupami i w ich obrębie: upuszczenie na projekt wstawia przed nim albo za nim, a na nagłówek grupy (także „Nieprzypisane projekty”) lub pustą grupę przenosi do niej. Puste grupy zostają widoczne z przyciskiem „Dodaj projekt…”, a „Rozwiąż grupę” zostawia projekty bez grupy.
- **Blokada nawigacji** (domyślnie włączona): karty projektu nie wyjdą poza jego katalog. Dotyczy to przycisku „w górę”, paska ścieżki, wpisanej lokalizacji, symlinków na zewnątrz i historii. Odmowa kończy się komunikatem z przyciskiem „Otwórz w nieprzypisanych kartach”. Wklejanie plików spoza projektu działa normalnie. To ułatwienie, a nie sandbox.
- **Sesja**: projekty, karty (z podziałem, podglądem i terminalem), zakładki projektów i rozmiar okna są przywracane przy starcie.
- **Upuszczanie plików na projekt** w sidebarze kopiuje je do katalogu projektu (z Shiftem przenosi).

### Jak w Nautilusie

- Ten sam wygląd (GTK 4 + libadwaita), widoki listy, drzewa i siatki, zoom, kolumny, miniatury ze wspólnej pamięci podręcznej.
- **Lewy sidebar** (F9): katalog projektu, Ostatnie, Ulubione, Zmienione pliki, zakładki projektu i zakładki systemowe, a na dole Kosz. Przy włączonej blokadzie zakładki systemowe spoza projektu są ukryte. W projekcie każde miejsce pokazuje tylko jego pliki. Tryb „Drzewo” pokazuje drzewo katalogów zsynchronizowane z kartą.
- **Operacje na plikach wykonuje sam Nautilus** przez D-Bus: kopiowanie, przenoszenie, kosz, zmiana nazwy, nowy katalog, cofnij/ponów. Okna postępu i konfliktów są więc jego, a historia cofania jest wspólna. Bez Nautilusa MetaFiles robi to samo lokalnie.
- **Właściwości** otwierają okno Nautilusa. Schowek działa w obie strony z Nautilusem: wytnij tam, wklej tutaj.
- **Ustawienia** czytane na żywo z Nautilusa: ukryte pliki, domyślny widok, sortowanie, kolumny, pojedyncze lub podwójne kliknięcie, miniatury.
- **Ulubione**: gwiazdki z Nautilusa są widoczne w MetaFiles. Gwiazdki dodane tutaj nie trafiają do Nautilusa, bo jego baza jest prywatna.
- **Zakładki**: wspólne z Nautilusem (`~/.config/gtk-3.0/bookmarks`). Ctrl+D w projekcie dodaje zakładkę projektu.

### Ponad Nautilusa

| Funkcja | Skrót |
|---|---|
| Szybkie otwieranie: rozmyte wyszukiwanie po projekcie | Ctrl+P |
| Szybki podgląd / panel podglądu (obrazy, kod, PDF, wideo, archiwa, katalogi) | Spacja / Alt+P |
| Dwa panele w karcie, przełączanie, kopiowanie i przenoszenie na drugą stronę | F3, F6, Shift+F5, Shift+F6 |
| Terminal w panelu zsynchronizowany z widokiem w obie strony | F4 |
| Ukrywanie plików ignorowanych przez Gita | Ctrl+Shift+H |
| Nowy pusty plik / duplikat | Ctrl+Alt+N / Ctrl+Shift+D |
| Kopiuj pełną / względną ścieżkę | Ctrl+Shift+C / Ctrl+Alt+C |

Do tego:
- **Git**: kolorowe statusy plików i katalogów, gałąź w sidebarze i pasku stanu, widok „Zmienione pliki”, a w menu Gita: diff, odrzucenie zmian, dodanie do indeksu i usunięcie z niego.
- **Zbiorcza zmiana nazw**: zaznacz kilka plików i naciśnij F2. Dostępne są wyrażenia regularne (`$1`), numeracja, wielkość liter i rozszerzenia, z podglądem i jednym cofnięciem.
- **Archiwa**: kompresja i rozpakowywanie tą samą biblioteką co w Nautilusie.
- **Inne**: rozmiary katalogów liczone w tle, pasek stanu z wolnym miejscem, porównywanie dwóch plików w Meld.
- **Terminal**: przy zmianie katalogu w widoku przechodzi tam tylko wtedy, gdy czeka na polecenie, a niedokończona komenda zostaje nietknięta. `cd` w terminalu przenosi widok. W zablokowanym projekcie `cd` poza projekt jest odrzucane, jak w MetaTerminalu.

### Akcje użytkownika

Własne polecenia w menu kontekstowym definiujesz w `~/.config/metafiles/config.json`, w sekcji `actions`. Przykład:

```json
{"label": "Otwórz w VS Code", "command": ["code", "%F"], "context": ["files", "folders"]}
```

- **Znaczniki**:
  - `%F`: zaznaczone ścieżki (każda jako osobny argument);
  - `%f`: pierwsza ścieżka;
  - `%d`: bieżący katalog;
  - `%p`: katalog projektu;
  - `%n`: nazwy plików.
- **Konteksty**: `files`, `folders`, `folder` (puste miejsce), `project` (menu projektu).
- **Pozostałe opcje**: `mime`, `extensions`, `min`, `max`, `shortcut`, `terminal`, `confirm`.

Polecenia uruchamiają się bez powłoki, a ścieżki nigdy nie są wklejane w tekst polecenia. Zmiany w pliku działają od razu.

## Skróty

Działają domyślne skróty Nautilusa 50 (Ctrl+T, Ctrl+W, Alt+←/→/↑, Ctrl+L, Ctrl+F, Ctrl+H, F2, Delete, Ctrl+Z…), z tym że „nowe okno” to „dodaj projekt”, a „katalog domowy” to katalog projektu. Pełna lista jest w oknie Ctrl+?.

Skróty projektów (do zmiany w `config.json`, sekcja `shortcuts`):

| Akcja | Domyślnie |
|---|---|
| poprzedni/następny projekt | Alt+Page Up / Alt+Page Down |
| projekt 1…9 | Ctrl+Alt+1…9 |
| nieprzypisane karty | Ctrl+Alt+0 |
| fokus: pliki ⇄ projekty | Ctrl+Tab |
| sidebar projektów | Ctrl+Shift+S |

Alt+↑/↓ zostały w Nautilusie („katalog wyżej”/„otwórz”), a Ctrl+Alt+↑/↓ przełączają obszary robocze GNOME. Dlatego projekty przełącza się Alt+Page Up/Down.

W sidebarze projektów ↑/↓ zmieniają projekt, Enter przechodzi do plików, Esc wraca, klawisz Menu otwiera menu projektu, P dodaje projekt, a G tworzy grupę. Gdy fokus jest w plikach, bieżący projekt ma tylko kolorową obwódkę; gdy jest w sidebarze, ma pełne tło.

## Rozwój

```bash
python3 -m unittest discover -s tests -t .
# Testy interfejsu w osobnym, niewidocznym kompozytorze (zrzuty ekranu trafiają do shots/):
mutter --headless --wayland --no-x11 --virtual-monitor 1600x1000 --wayland-display mf-test &
WAYLAND_DISPLAY=mf-test python3 tests/harness.py features_tour shots
```

Scenariusze (`basic`, `features_tour`, `panels_tour`) są w `tests/scenarios.py`. Bez podanej ścieżki projektu harness tworzy przykładowy projekt w katalogu tymczasowym.

## Licencja

MIT, zobacz [LICENSE](LICENSE).

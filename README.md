# Gazetka Hebe → Excel

Skrypt `hebe.py` przy każdym uruchomieniu znajduje aktualną gazetkę na stronie
Hebe, pobiera jej PDF oraz identyfikatory z klikalnych ikon koszyka i tworzy
plik `.xlsx` z trzema kolumnami:

| ID | Data obowiązywania | Promocja |
| --- | --- | --- |
| 740207 | 05.11.2026–13.11.2026 | Cena promocyjna: 39,99 zł |

Nazwy produktów ani ich strony nie są pobierane. Daty, ceny i warunki promocji
pochodzą z gazetki. Skrypt nie ma wpisanego na stałe adresu konkretnego wydania,
liczby stron, dat ani listy ID. Nie wymaga klucza API ani płatnej usługi.

## Instalacja

Wymagane: Python 3.10+ i Google Chrome. W katalogu projektu:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Jeśli nie masz Chrome, zainstaluj przeglądarkę Playwright i wybierz ją przy
uruchamianiu:

```sh
.venv/bin/python -m playwright install chromium
.venv/bin/python hebe.py --browser-channel chromium
```

## Uruchamianie

```sh
.venv/bin/python hebe.py
```

Wynik: `outputs/<data-i-czas>/hebe.xlsx`. Każde uruchomienie tworzy osobny katalog.
Skrypt otwiera osobne okno Chrome i zamyka je po pobraniu danych. Jeśli Hebe
wyświetli weryfikację przeglądarki, wykonaj ją w tym oknie. Nie korzysta z Twojego
profilu Chrome ani zapisanych kont.

Można wskazać własną nazwę pliku:

```sh
.venv/bin/python hebe.py --output outputs/promocje.xlsx
```

Można wybrać inną gazetkę ze strony Hebe:

```sh
.venv/bin/python hebe.py --url 'https://www.hebe.pl/gazetka-hebe?magazine=plachta'
```

Tryb `--headless` ukrywa okno, ale Hebe może go blokować.

## Rozpoznawanie i ograniczenia

ID pochodzą bezpośrednio z warstwy koszyków iPaper. W Excelu są tekstem,
bez technicznych zer na początku. Zapis dotyczy produktów z klikalnym koszykiem;
nie rozszerza promocji na wszystkie produkty danej marki w sklepie.

Skrypt analizuje położenie tekstu, zdjęć, linii i ramek w PDF. Rozpoznaje ceny
promocyjne, warunki typu „drugi produkt -60%”, okresy z nagłówków oraz wyjątki
z przypisów. Zachowuje osobne wiersze, gdy to samo ID ma różne oferty.

To parser układu PDF, nie pełna interpretacja obrazu. **Wiersze o niejednoznacznym
przypisaniu są żółte i zawierają „DO SPRAWDZENIA” wraz z numerem strony.**
Wartości w tych wierszach są propozycją do weryfikacji. Brakująca promocja lub data
jest oznaczona wprost. Całkowicie graficzny PDF lub zmiana szablonu może wymagać
rozbudowania parsera; nie można zagwarantować poprawnego odczytu dowolnego
przyszłego układu. Skrypt nie zastępuje nierozpoznanej oferty bieżącą ceną online.

Obok Excela powstaje katalog `<nazwa>-dane` z PDF, `source.json` i `offers.json`.
Zawiera źródła oraz numer strony dla każdego ID. Można ponownie przeliczyć
zachowane wydanie bez pobierania go z internetu:

```sh
.venv/bin/python hebe.py --replay outputs/<data-i-czas>/hebe-dane
```

`--strict` zwraca kod zakończenia `2`, jeśli jakiś wiersz wymaga sprawdzenia;
Excel nadal jest zapisywany. Błąd pobierania zwraca `1`. Zwykłe zakończenie ma kod
`0`, a liczba wierszy do sprawdzenia zawsze pojawia się w logu.
`--json-only` pomija zapis Excela.

## Testy

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

# Gazetka Hebe → Excel

Skrypt `hebe.py` przy każdym uruchomieniu znajduje aktualną gazetkę na stronie
Hebe, pobiera jej PDF oraz identyfikatory z klikalnych ikon koszyka i tworzy
plik `.xlsx` z jednym arkuszem **Promocje**. Pierwsze cztery kolumny to:

| Produkt | ID | Data obowiązywania | Promocja |
| --- | --- | --- | --- |
| &honey Melty Moist Repair — odżywka do włosów puszących się, 445 g | 740207 | 05.11.2026–13.11.2026 | Cena z gazetki: 39,99 zł |

Nazwy, opisy wariantów, marka, bieżące ceny i etykiety promocji są pobierane po ID
z publicznego API Hebe `POST /productsInfo?products=ID1,ID2,...`, po 20 produktów
w jednym zapytaniu. Odpowiedzi są łączone po ID, niezależnie od kolejności.
Brakujące lub nieprawidłowe rekordy są uzupełniane z QuickView, czyli podglądu
otwieranego koszykiem w gazetce. Nie trzeba kopiować cookies ani nagłówków sesji
z przeglądarki. Nazwa w Excelu prowadzi przez adres produktu z jego ID.
Kliknięcie nazwy w Excelu otwiera stronę produktu. Daty, ceny i pełne warunki
eksportowanej promocji pochodzą z gazetki. Kolejne kolumny w tym samym arkuszu zawierają porównanie oferty online, bieżącą
cenę i etykiety, flagę promocji cenowej online oraz numer strony gazetki.
Łącznie arkusz zawiera osiem kolumn. Nie ma osobnego arkusza
Weryfikacja. Nazwa produktu prowadzi do jego strony, a bieżąca oferta online —
do źródła porównania. Nagłówek oraz kolumny Produkt i ID są zablokowane podczas
przewijania; tabela pozwala filtrować także po wyniku weryfikacji.
Skrypt nie ma wpisanego na stałe adresu konkretnego wydania,
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

`--skip-names` ukrywa nazwy i opisy w arkuszu Promocje. Dane z API są nadal
pobierane, ponieważ tożsamość produktu jest potrzebna do przypisania promocji.

Powtórzenia tego samego ID korzystają ze wspólnego wyniku pobierania. Dla
skróconych ID skrypt próbuje też formatu z zerami na początku wymaganego przez
katalog Hebe. Sprawdza zarówno `pid`, jak i ID w danych produktu. Rekord z innym
ID, pustą nazwą, brakującą ceną lub niepotwierdzoną walutą nie jest przyjmowany.
QuickView używany jako źródło zapasowe ma najwyżej trzy równoległe zapytania
i sprawdza również ID w linku produktu. Brak nazwy, usunięty produkt albo blokada Hebe nie usuwają
wiersza: zamiast nazwy widnieje „ID <numer>”, a przyczyna jest dopisana do
pola „DO SPRAWDZENIA”. Nie zgaduje nazw z sąsiednich produktów w PDF.

## Rozpoznawanie i ograniczenia

ID pochodzą bezpośrednio z warstwy koszyków iPaper. Ta warstwa nie udostępnia
cen ani warunków promocji (pole ceny w aktualnie sprawdzanym wydaniu ma wartość
techniczną `0`). W Excelu ID są tekstem,
bez technicznych zer na początku. Zapis dotyczy produktów z klikalnym koszykiem;
nie rozszerza promocji na wszystkie produkty danej marki w sklepie.

Skrypt wiąże ID z opisem w PDF na podstawie marki, nazwy, opisu i pojemności
potwierdzonych w API lub QuickView. Położenie tekstu rozstrzyga między pasującymi opisami.
Jeden opis może obejmować kilka wariantów, np. tusz czarny i brązowy opisane jako
„różne rodzaje”. Brak zgodnej tożsamości lub konflikt wariantów powoduje
wstrzymanie przypisania ceny. Skrypt nie przesuwa produktu do wolnego opisu
sąsiada. Promocje określonej marki wymagają zgodności marki; ogólne kampanie
korzystają z wydzielonego panelu gazetki. Rozpoznaje ceny drukowane, warunki typu
„drugi produkt -60%”, okresy z nagłówków oraz wyjątki z przypisów. Zachowuje osobne
wiersze, gdy to samo ID ma różne oferty.

PDF z zaznaczalnym tekstem jest pobierany automatycznie przy każdym uruchomieniu.
Daty mają następującą kolejność pierwszeństwa:

1. Przypis przypisany do produktu przez dokładną liczbę gwiazdek (`*`, `**`, `***` itd.).
2. Przypis kampanii lub przypis wewnątrz konkretnego pola produktu.
3. Daty w nagłówku właściwego panelu strony.
4. Ogólna klauzula „Oferta obowiązuje w dniach…” z warunków gazetki.

Przypisy są dzielone na osobne klauzule nawet wtedy, gdy PDF umieszcza je
w jednym wierszu lub bloku tekstu. Pozycje znaków odróżniają przypisy z różnych
kolumn. Ten sam `*` może oznaczać różne okresy w różnych polach produktu.
Osobno zapisany indeks górny też może zostać połączony z opisem produktu.
Klauzule „Promocja nie łączy się…” i „Marka dostępna…” są zachowywane jako
powiązane warunki, ale nie zmieniają dat obowiązywania. Nieodczytany przypis
produktu lub sprzeczne daty powodują oznaczenie daty do sprawdzenia.
W Excelu jest numer strony. Cytaty przypisów, liczba gwiazdek, pozycja klauzuli
i warunek wyczerpania zapasów są zapisane w `offers.json`.

Sama obecność produktu i ceny w PDF nie oznacza obniżki. Samodzielne ceny mają
neutralny opis **„Cena z gazetki”**, również gdy cena jest zgodna z API. Warunki
rabatu, np. „drugi produkt -60%”, pozostają opisane wprost. API `onPromotion`
jest zapisywane w JSON jako `online_on_promotion` i wyświetlane w kolumnie
„Promocja cenowa online” jako Tak/Nie/Brak danych. `false` oznacza brak oznaczenia promocji cenowej w pobranej ofercie
online; nie usuwa produktu ani osobnej kampanii z gazetki. Brak flagi to brak
danych, a nie `false`. Daty przy samej cenie oznaczają okres oferty z gazetki,
a nie potwierdzenie okresu obniżki.

Każdy wiersz jest następnie porównywany z ofertą online. Kolumna „Porównanie oferty online” rozróżnia:

- zgodną cenę lub zgodny mechanizm rabatu na drugi produkt,
- inną ofertę online jako informację o różnicy między źródłami,
- promocję zakończoną lub przyszłą względem daty pobrania danych online,
- konflikt tożsamości produktu lub brak danych do porównania.

API i QuickView pokazują **bieżącą ofertę online**. Mogą zawierać inny kod rabatowy,
cenę internetową albo ofertę innego kanału sprzedaży. Nie udostępniają
dat konkretnej gazetki, a zgodna etykieta rabatu nie potwierdza wszystkich jej
warunków. Bieżąca cena nie zastępuje ceny drukowanej. ID kampanii nie jest
interpretowane jako okres promocji. Oferty zakończone i przyszłe nie są
automatycznie uznawane za błędne z powodu innej bieżącej ceny.

**Żółty wiersz i „DO SPRAWDZENIA” oznaczają problem z odczytem lub przypisaniem
oferty z gazetki:** brak daty, brak promocji, niejednoznaczny produkt albo konflikt
przypisów. Różnica ceny lub promocji online pozostaje informacją w kolumnach weryfikacji i nie wywołuje żółtego ostrzeżenia. Sam brak danych online też nie
oznacza błędu poprawnie odczytanej oferty. W tym wydaniu warunki gazetki wskazują
ceny wybranych sklepów stacjonarnych, z osobnym oznaczeniem ofert online.

Błędny link w samej gazetce
też może wymagać wyjaśnienia: np. koszyk przy jednym modelu koca może otwierać
inny model tej samej marki. Takiemu ID nie jest przypisywana cena sąsiedniego
opisu. Brakująca promocja lub data
jest oznaczona wprost. Całkowicie graficzny PDF lub zmiana szablonu może wymagać
rozbudowania parsera; nie można zagwarantować poprawnego odczytu dowolnego
przyszłego układu. Skrypt nie zastępuje nierozpoznanej oferty bieżącą ceną online.

Do porównania ceny API służy `priceObj.SalesPrice`. Cena omnibus, cena regularna,
widełki `min/max` ani warunkowa cena z kodem, kartą lub statusem VIP nie zastępują
tej wartości. Dodatkowe ceny są zachowane w źródle. Mechanizm rabatu jest
odczytywany także z `badge.everyonePromotionBadge.calloutMsg`, jeśli brakuje go
w krótkiej etykiecie.

Obok Excela powstaje katalog `<nazwa>-dane` z PDF, `source.json`, `offers.json`,
folderem `productsInfo/` z surowymi odpowiedziami JSON i, jeśli użyto źródła
zapasowego, folderem `quickview/` z HTML. W `source.json` są zapisane odczytane
ceny, etykiety, czas pobrania i skrót SHA-256 każdej odpowiedzi. `offers.json`
zawiera dopasowany opis i jego współrzędne, źródło i dowód odczytania daty,
numer koszyka, porównanie dla każdego wiersza oraz podsumowanie weryfikacji.
Można ponownie przeliczyć
zachowane wydanie bez pobierania go z internetu:

```sh
.venv/bin/python hebe.py --replay outputs/<data-i-czas>/hebe-dane
```

Odtwarzanie wydania działa bez internetu i korzysta z daty zapisanej odpowiedzi,
nie z daty odtworzenia. Starsze archiwa bez odpowiedzi API ani QuickView są oznaczane
jako nieweryfikowalne. Aby zapisać komplet danych, wykonaj nowe uruchomienie online.

`--strict` zwraca kod zakończenia `2`, jeśli odczyt lub przypisanie oferty z gazetki
wymaga sprawdzenia. Same różnice oferty online nie powodują kodu `2`;
Excel nadal jest zapisywany. Błąd pobierania zwraca `1`. Zwykłe zakończenie ma kod
`0`, a liczba wierszy do sprawdzenia zawsze pojawia się w logu.
`--json-only` pomija zapis Excela.

## Testy

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

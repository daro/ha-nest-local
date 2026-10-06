# Nest Local (NoLongerEvil) – integracja Home Assistant

Home Assistant sam udaje chmurę Nest. Termostat Nest Learning (gen 1/2) z firmware
[NoLongerEvil](https://github.com/codykociemba/NoLongerEvil-Thermostat) łączy się bezpośrednio
z serwerem wbudowanym w tę integrację – bez serwera NLE, bez MQTT, bez dodatkowych kontenerów.

```
Nest (firmware NLE) ──HTTP, long-poll──▶ Home Assistant :9544 ──▶ climate / sensory / przełączniki
```

## Co dostajesz

| Encja | Opis |
|---|---|
| `climate` | tryb (wył./grzanie/chłodzenie/auto – zależnie od okablowania), temperatura zadana, preset **eco**, wilgotność, akcja HVAC, wentylator (jeśli jest) |
| `sensor` | temperatura, wilgotność, bateria (% i napięcie), czas osiągnięcia temperatury, temperatura eco, adres IP, sygnał Wi‑Fi, temperatura podstawy; dla Heat Link z OpenTherm także temperatura wody, nastawa i modulacja kotła |
| `binary_sensor` | łączność, grzanie, chłodzenie, wentylator, obecność (czujnik ruchu Nesta), ciepła woda, płomień kotła |
| `switch` / `select` | podgrzewanie ciepłej wody (boost) i tryb ciepłej wody – tylko gdy termostat steruje CWU (Heat Link) |

Encje pojawiają się, gdy termostat wyśle swój stan. Te, których Twój termostat nie obsługuje, nie są tworzone.

Do tego [harmonogram tygodniowy](#harmonogram-tygodniowy) ustawiany z pomocnika *Harmonogram* w HA.

## Wymagania

- Nest Learning Thermostat gen 1 lub 2 z wgranym firmware NoLongerEvil (do przepięcia potrzebne
  jest lokalne API firmware albo SSH – patrz niżej).
- Home Assistant 2026.1 lub nowszy (testowane na 2026.2 i 2026.9).
- Termostat i Home Assistant w tej samej sieci. Ustaw rezerwację DHCP dla HA i dla termostatu.

## Instalacja

### Przez HACS

1. HACS → ⋮ (prawy górny róg) → **Custom repositories**.
2. Repozytorium: `https://github.com/daro/ha-nest-local`, typ: **Integration** → **Add**.
3. Wyszukaj w HACS **Nest Local** → **Download**.
4. Zrestartuj Home Assistanta.

Nowe wersje (wydania na GitHubie) HACS pokaże jako aktualizację.

### Ręcznie

Skopiuj katalog `custom_components/nest_local` do `/config/custom_components/` w Home Assistancie
(np. przez aplikację Samba share, Studio Code Server albo SSH) i zrestartuj Home Assistanta.

### Konfiguracja

1. **Ustawienia → Urządzenia i usługi → Dodaj integrację → Nest Local**.
2. Podaj adres IPv4 Home Assistanta (nie `homeassistant.local` – termostat nie rozwiązuje takich nazw)
   i port (domyślnie **9544**; 9543 zajmuje aplikacja NoLongerEvil, jeśli nadal działa).

## Przepięcie termostatu na Home Assistanta

Ustawienie `cloudregisterurl` termostatu ma wskazywać `http://IP-HA:9544/entry`.

### Skryptem (macOS / Linux)

Z klonu tego repozytorium, na komputerze w tej samej sieci co termostat:

```sh
bash tools/nest-to-ha.sh 192.168.1.50      # IP termostatu z listy DHCP
```

Skrypt:

1. sprawdza, czy integracja w HA odpowiada,
2. prosi o obudzenie termostatu (uśpiony Nest nie odpowiada w sieci) i sprawdza ping, lokalne API
   firmware (port 8080) i SSH,
3. ustawia `cloudregisterurl` przez lokalne API, a gdy go nie ma – przez SSH (hasło root,
   domyślnie `nolongerevil`),
4. zapisuje poprzedni adres w `~/nest-backup-NUMER.txt` i czeka, aż termostat zgłosi się w HA.

Gdy przepięcie się nie uda, skrypt mówi, co jest nie tak (termostat śpi, jest w innej sieci,
ma za stary firmware) i co z tym zrobić.

| Polecenie | Co robi |
|---|---|
| `bash tools/nest-to-ha.sh --check IP` | tylko diagnostyka, niczego nie zmienia |
| `bash tools/nest-to-ha.sh --restore` | przywraca poprzedni adres z kopii |
| `bash tools/nest-to-ha.sh -h` | wszystkie opcje |

Domyślny adres integracji w skrypcie to `192.168.1.10:9544`; inny podaj opcjami `-a` i `-p`.

### Ręcznie

> Najpierw **zapisz obecną wartość `cloudregisterurl`** – to Twoja droga powrotu.

**Sposób A – lokalne API firmware NLE** (port 8080, firmware NLE od grudnia 2025):

```sh
NEST=IP-TERMOSTATU
curl -s http://$NEST:8080/cgi-bin/api/settings      # obecny adres – zapisz go
curl -s -X POST -d '{"initialize":"NUMER_SERYJNY"}' http://$NEST:8080/cgi-bin/api/settings
curl -s -X POST -d '{"api_key":"KLUCZ","endpoint":"http://IP-HA:9544"}' http://$NEST:8080/cgi-bin/api/settings
```

Numer seryjny to nazwa hosta termostatu (lista DHCP albo Settings > Technical Info). Pierwszy
POST zwraca `api_key`, drugi `"status":"new"`. Termostat sam dopisze `/entry` i po ok. 10 s
zrestartuje swoje oprogramowanie.

**Sposób B – SSH** (firmware NLE od połowy listopada 2025):

```sh
ssh root@IP-TERMOSTATU          # domyślne hasło: nolongerevil (chyba że ustawiłeś własne)
vi /etc/nestlabs/client.config
#   <a key="cloudregisterurl" value="http://IP-HA:9544/entry"/>
reboot
```

**Starszy firmware NLE** (bez API i SSH): wgraj aktualny instalatorem NoLongerEvil (wersja 1.0.1
lub nowsza). W kreatorze wybierz Self-Hosted, potem zakładkę **NLE Server** (nie Home Assistant –
ta zawsze ustawia port 9543 dodatku NLE) i wpisz adres HA oraz port integracji (9544).

### Po przepięciu

Pełny stan termostat wysyła po restarcie, więc jeśli encje nie pojawią się w ciągu kilku minut,
zrestartuj go (przytrzymaj ekran ok. 10 s). Potem w Home Assistancie pojawi się urządzenie
*Nest Thermostat* (lub nazwa pokoju z ustawień Nesta).

## Harmonogram tygodniowy

Termostat ma własny harmonogram tygodniowy i wykonuje go sam, także gdy Home Assistant nie działa.
Integracja może go ustawiać z pomocnika **Harmonogram** w HA – na przykład w poniedziałek grzanie
od 10:00 do 18:00, a we wtorek od 7:00 do 9:00 i od 17:00 do 22:00:

1. **Ustawienia → Urządzenia i usługi → Pomocnicy → Utwórz pomocnika → Harmonogram**. Zaznacz
   okresy grzania w poszczególne dni.
2. **Nest Local → ⚙ (opcje)**: wybierz ten harmonogram oraz temperaturę w okresach grzania
   (np. 21 °C) i poza nimi (np. 16 °C).

Początek każdego okresu staje się punktem harmonogramu Nesta z temperaturą grzania, koniec – punktem
z temperaturą poza okresami. Integracja zapisuje cały tydzień w termostacie, a każdą zmianę
pomocnika wysyła po kilku sekundach.

- Okres może mieć własną temperaturę: w jego dodatkowych danych ustaw `temperature: 22` (°C).
- Harmonogram z HA zastępuje harmonogram termostatu: zmiany zrobione na termostacie zostaną
  nadpisane, a Auto-Schedule (uczenie się harmonogramu) wyłączony. Plan trafia do wszystkich
  termostatów podłączonych do integracji.
- Godziny liczy zegar termostatu: sprawdź, czy pokazuje tę samą godzinę co Home Assistant
  (ta sama strefa czasowa).
- Działa, gdy termostat używa harmonogramu grzania. Wyłączony albo offline termostat dostanie
  harmonogram później – po włączeniu grzania albo po powrocie do sieci.
- Pusty pomocnik oznacza temperaturę spoza okresów przez cały tydzień. Bez wybranego pomocnika
  integracja nie zmienia harmonogramu termostatu.
- Akcja `nest_local.get_schedule` (Narzędzia deweloperskie → Akcje) pokazuje harmonogram zapisany
  w termostacie.

## Opcje

- **Konfiguruj → zmień adres/port** (reconfigure). Po zmianie zaktualizuj `cloudregisterurl`
  (np. `bash tools/nest-to-ha.sh -p NOWY_PORT IP-TERMOSTATU`).
- **Opcje**: przekazywanie zapytań o pogodę do `weather.nest.com` (temperatura na zewnątrz na ekranie
  termostatu), długość podgrzewania wody i [harmonogram tygodniowy](#harmonogram-tygodniowy).

## Co warto wiedzieć

- **Temperatura ustawiona z HA** działa jak przekręcenie pokrętła: obowiązuje do następnego punktu
  harmonogramu termostatu. Integracja oznacza każdą zmianę tak, jak robiła to chmura Nest
  (z HA – zdalna, pokrętłem – ręczna), dzięki czemu termostat poprawnie pokazuje tymczasową zmianę.
- **Zegar termostatu**: gdy zmienisz harmonogram na termostacie, integracja porównuje jego strefę
  czasową ze strefą HA i ostrzega w logu, jeśli się różnią (harmonogram liczy godziny według
  zegara termostatu).
- **Eco**: preset *eco* w HA włącza ręczny tryb eco Nesta (`manual_eco_all`); *none* go wyłącza.
  Atrybut `eco_mode` pokazuje, czy eco jest ręczne czy automatyczne.
- **Termostat offline** (np. słaba bateria): zmiany z HA czekają w kolejce i zostaną wysłane po
  ponownym połączeniu – zmiana temperatury/trybu najwyżej 30 min, inne zmiany do 6 h.
  Atrybut `waiting_for_thermostat` pokazuje, że coś jeszcze nie dotarło.
- **Bezpieczeństwo**: serwer na porcie 9544 nie ma uwierzytelniania (tak działa protokół Nest).
  Nie wystawiaj tego portu do internetu.
- **Pogoda** pochodzi z usługi Google `weather.nest.com`; jeśli przestanie działać, wyłącz opcję.
- **Przejście z NoLongerEvil**: jeśli termostat był sparowany z NLE (hosted lub aplikacja),
  integracja przejmuje istniejące parowanie zamiast tworzyć nowe. Aplikację NLE możesz po przepięciu
  zatrzymać.
- **Ikona** integracji (katalog `brand/`) pokazuje się w Home Assistancie 2026.3 i nowszym.

## Stan projektu

Integracja implementuje protokół zgodnie z działającym serwerem NoLongerEvil i dokumentacją
[nest-thermostat-protocol-docs](https://github.com/cjserio/nest-thermostat-protocol-docs).
Jest przetestowana automatycznie (symulowany termostat po prawdziwym HTTP, skrypt przepinający
na modelu API firmware NLE), ale **nie była jeszcze uruchamiana z fizycznym termostatem**.
Przy pierwszym uruchomieniu:

- zostaw sobie możliwość powrotu (stara wartość `cloudregisterurl`; skrypt zapisuje ją sam,
  a `--restore` ją przywraca),
- włącz logi debug i sprawdź, czy zmiany z HA docierają do termostatu w ciągu kilku sekund.

## Diagnostyka

Logi debug (`configuration.yaml`):

```yaml
logger:
  logs:
    custom_components.nest_local: debug
```

**Ustawienia → Urządzenia i usługi → Nest Local → ⋮ → Pobierz diagnostykę** – zawiera stan
wszystkich „bucketów” termostatu, oczekujące zmiany i stan połączenia (adresy są ukryte).

## Testy (dla programistów)

```sh
pip install -r requirements_test.txt
pytest
```

## Licencja i podziękowania

MIT. Wiedza o protokole pochodzi z projektu NoLongerEvil (Hack House, MIT) oraz
dokumentacji protokołu autorstwa cjserio (MIT). Projekt nie jest powiązany z Google ani Nest.

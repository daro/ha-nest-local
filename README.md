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

## Wymagania

- Nest Learning Thermostat gen 1 lub 2 z wgranym firmware NoLongerEvil (dostęp SSH do termostatu).
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

> Najpierw **zapisz obecną wartość `cloudregisterurl`** – to Twoja droga powrotu.

**Sposób A – przeglądarka (API firmware NLE):**
otwórz `http://IP-TERMOSTATU:8080/cgi-bin/api/settings` i ustaw `cloudregisterurl` na
`http://IP-HA:9544/entry`. Jeśli termostat działa dłużej niż 30 minut, strona zapyta o hasło –
jest w pliku `/etc/nestlabs/apikey.txt` na termostacie.

**Sposób B – SSH:**

```sh
ssh root@IP-TERMOSTATU          # domyślne hasło: nolongerevil (chyba że ustawiłeś własne)
vi /etc/nestlabs/client.config
#   <a key="cloudregisterurl" value="http://IP-HA:9544/entry"/>
reboot
```

**Zrestartuj termostat** (jeśli nie zrobił tego `reboot`: przytrzymaj ekran ok. 10 s).
Pełny stan termostat wysyła tylko po restarcie – bez tego encje się nie pojawią.
Po chwili w Home Assistancie pojawi się urządzenie *Nest Thermostat* (lub nazwa pokoju z ustawień Nesta).

## Opcje

- **Konfiguruj → zmień adres/port** (reconfigure). Po zmianie zaktualizuj `cloudregisterurl` i zrestartuj termostat.
- **Opcje**: przekazywanie zapytań o pogodę do `weather.nest.com` (temperatura na zewnątrz na ekranie
  termostatu) oraz długość podgrzewania wody.

## Co warto wiedzieć

- **Harmonogram zostaje na termostacie.** Temperatura ustawiona z HA działa jak przekręcenie pokrętła:
  obowiązuje do następnego punktu harmonogramu Nesta. Jeśli chcesz, żeby harmonogram prowadził HA,
  wyczyść harmonogram na termostacie i ustawiaj temperaturę automatyzacjami.
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

## Stan projektu

Integracja implementuje protokół zgodnie z działającym serwerem NoLongerEvil i dokumentacją
[nest-thermostat-protocol-docs](https://github.com/cjserio/nest-thermostat-protocol-docs).
Jest przetestowana automatycznie (66 testów, w tym symulowany termostat po prawdziwym HTTP),
ale **nie była jeszcze uruchamiana z fizycznym termostatem**. Przy pierwszym uruchomieniu:

- zostaw sobie możliwość powrotu (stara wartość `cloudregisterurl`),
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

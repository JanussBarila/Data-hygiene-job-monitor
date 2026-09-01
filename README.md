# DATA HIGIENE — vakanču atlase

Python projekts Latvijas darba piedāvājumu atlasei, izmaiņu vēsturei un e-pasta paziņojumiem. Tas izmanto CSP cilvēkkapitāla MCP serverī saglabātos CV.lv sludinājumus un sagatavo latviski noformētu vakanču izlasi.

## Iespējas

- Filtri pēc mēnešalgas apakšējās robežas, pilsētas, atslēgvārdiem un pēdējā novērojuma datuma.
- Pašreizējās atlases, jaunu vakanču un izmaiņu eksports CSV failos.
- Vēstures saglabāšana SQLite datubāzē.
- Atsevišķi e-pasta paziņojumi vairākiem saņēmējiem; nosūtītās vakances tiek uzskaitītas katram adresātam.
- DATA HIGIENE HTML vēstule latviešu valodā un vienkārša teksta alternatīva.
- Vienreizējs tests uz savu adresi un priekšskatījums bez nosūtīšanas.
- Windows uzdevumu plānotāja skripti ikdienas palaišanai.

“Jauna” nozīmē pirmoreiz atrasta vai attiecīgajam adresātam vēl nenosūtīta vakance — atkarībā no pārskata. Tā ne vienmēr ir šodien publicēta. Pirms pieteikšanās sludinājuma pieejamība jāpārbauda CV.lv.

## Prasības un palaišana

Python 3.10 vai jaunāks; izmantota standarta bibliotēka. Pasta iestatījumu logs izmanto Tkinter, bet paroles glabāšana un uzdevumu plānotājs — Windows. Lai ielādētu vakances un nosūtītu e-pastu, vajadzīgs internets.

Piemēri PowerShell terminālī, atrodoties projekta mapē, ja `uv` ir pieejams:

```powershell
uv run .\job_tracker.py
```

Tas atjaunina atlasi un vēsturi. Filtri atrodas `job_filter_mcp.py`.

Pasta iestatījumus atver ar dubultklikšķi uz `Email_Settings.cmd` Windows failu pārlūkā. Saglabā savu sūtītāja adresi, saņēmējus un pasta lietotnes paroli. Paroli neievieto programmas kodā vai README.

Pēc pasta iestatīšanas:

```powershell
# Atjaunināt datus un apskatīt vēstuli, to nenosūtot
uv run .\job_tracker_email.py --preview
if ($LASTEXITCODE -eq 0) { Invoke-Item .\email_test_preview.html }

# Nosūtīt vienu testa vēstuli uz sūtītāja adresi no iestatījumiem
uv run .\job_tracker_email.py --test

# Parastā nosūtīšana: katram saņēmējam tikai vēl nenosūtītās vakances
uv run .\job_tracker_email.py
```

Ja PowerShell neatrod `uv`, komandas sākumu aizstāj ar:

```powershell
& "$env:USERPROFILE\.local\bin\uv.exe" run .\job_tracker.py
```

Ikdienas uzdevuma iestatīšana aprakstīta [vakanču vēstures instrukcijā](job_tracker_README.md), pasta pieslēgšana un kļūdu diagnostika — [pasta instrukcijā](README_email.md). Esošam, strādājošam projektam tikai saglabāšanas GitHub dēļ uzdevumu nav jāinstalē atkārtoti.

## Galvenie faili

| Fails | Nozīme |
| --- | --- |
| `job_filter_mcp.py` | MCP pieprasījumi, SQL atlase un filtri |
| `job_tracker.py` | Vēsture, salīdzinājums un CSV eksports |
| `vacancies_email.py` | Vēstuļu noformējums un pasta nosūtīšana |
| `job_tracker_email.py` | Datu atjaunināšana un e-pasta režīmu palaišana |
| `email_settings.py` | Pasta iestatījumu logs |
| `Email_Settings.cmd`, `open_email_settings.ps1` | Iestatījumu loga atvēršana |
| `install_job_schedule.ps1` | Windows ikdienas uzdevuma iestatīšana |
| `install_email_notifications.ps1` | Pasta pieslēgšana esošajam uzdevumam |

## Saglabāšana GitHub no VS Code

Šo README un `.gitignore` pievieno esošajai projekta mapei blakus `job_tracker.py`. Tie paredzēti Tava pašreizējā koda saglabāšanai; šajā sagatavošanas komplektā nav programmas failu aizvietojumu.

1. Pārbaudi, vai datorā ir [Git](https://git-scm.com/download/win): PowerShell komandai `git --version` jāparāda versija. Pēc Git instalēšanas pārstartē VS Code.
2. VS Code atver esošo projekta mapi: **File → Open Folder**.
3. Atver **Source Control** ar **Ctrl+Shift+G**. Ja redzama tikai **Initialize Repository**, vispirms nospied to.
4. Izvēlies **Publish to GitHub**. Ja poga nav redzama, meklē `Publish to GitHub` komandu ar **Ctrl+Shift+P**.
5. Pieslēdzies savam GitHub kontam pārlūkā. Ja vajadzīgs, izveido kontu [GitHub](https://github.com/signup).
6. Izvēlies **Private** un repozitorija nosaukumu `data-higiene-vacancies`.
7. Pārskati pirmā commit failus. Tajos jābūt programmas kodam, dokumentācijai un `.gitignore`. Pasta iestatījumu JSON, datubāzēm, CSV eksportiem, žurnāliem un vēstuļu kopijām sarakstā nav jābūt.
8. Pabeidz publicēšanu un atver repozitoriju GitHub. Pārbaudi, vai redzami `job_filter_mcp.py`, `job_tracker.py` un pasta moduļi.

Pēc turpmākām koda izmaiņām: saglabā failus → **Source Control** → atzīmē izmaiņas ar **+** → ievadi īsu aprakstu → **Commit** → **Sync Changes**. Tikai `Ctrl+S` saglabā failu datorā; GitHub izmaiņas parādās pēc nosūtīšanas.

Ja Git pirmoreiz prasa autora vārdu un e-pastu, iestati tos lokāli šim projektam VS Code terminālī. Pirms komandu izpildes aizstāj piemēra vērtības ar savējām; e-pastam vari izmantot GitHub sadaļā **Settings → Emails** norādīto `noreply` adresi.

```powershell
git config user.name "Tavs vārds"
git config user.email "tava-github-adrese@example.com"
```

## Kas paliek datorā

Pievienotais `.gitignore` atļauj Git uzskaitīt tikai konkrēti nosauktos avota un dokumentācijas failus. Ja vēlāk pievieno jaunu Python moduli, papildini `.gitignore` ar rindu `!/jauns_modulis.py`, lai to arī saglabātu.

Pasta konfigurācija, saņēmēju adreses, šifrētā parole, vēstures datubāzes, eksporti, žurnāli un vēstuļu kopijas netiek iekļautas jaunā repozitorijā. `.gitignore` neizņem failus, kas Git jau ir uzskaitīti; tādēļ pārbaudi pirmā commit failu sarakstu. Šīs instrukcijas izmanto Git caur VS Code, nevis visas darba mapes vilkšanu GitHub pārlūka augšupielādē.

GitHub saglabā kodu un tā versijas. Tas pats par sevi nepārņem ikrīta palaišanu — tā turpina darboties Tavā Windows datorā.

Šis ir koda saglabāšanas komplekts, nevis pilna darba vides rezerves kopija. Pārceļoties uz citu datoru, atsevišķi privāti pārnes `vacancies_history.sqlite3` un `vacancies_email.sqlite3`, lai saglabātu atrasto un nosūtīto vakanču vēsturi. Pasta paroli jaunajā datorā ievadi vēlreiz un atjauno Windows uzdevumu.

## Datu avots

- [CSP cilvēkkapitāla MCP serveris](https://mcp-hc.stat.gov.lv/); sākotnējie sludinājumi — CV.lv.
- Datu licences norāde: [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
- Vakanču dati tiek atlasīti, normalizēti un pārformatēti pārskatiem. Sludinājumu nosaukumi saglabāti avota valodā.

GitHub instrukciju avoti: [publicēšana no VS Code](https://code.visualstudio.com/docs/sourcecontrol/repos-remotes#publish-to-github), [Git iestatīšana VS Code](https://code.visualstudio.com/docs/sourcecontrol/github#prerequisites), [failu ignorēšana](https://docs.github.com/en/get-started/git-basics/ignoring-files).

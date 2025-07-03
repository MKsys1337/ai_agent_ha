# AI Agent HA - Multilinguale Version

## Übersicht

Diese erweiterte Version der AI Agent HA Integration bietet vollständige deutsche Lokalisierung und ermöglicht es dir, die Integration sowohl auf Deutsch zu verwenden als auch deutsche Antworten von den KI-Modellen zu erhalten.

## 🆕 Neue Features

### 1. Deutsche Benutzeroberfläche
- **Vollständig übersetzte Konfigurationsoberfläche** mit "Du"-Anrede für moderne, benutzerfreundliche Kommunikation
- **Deutsche Service-Beschreibungen** in der Entwicklertools-Sektion
- **Lokalisierte Fehlermeldungen** und Hilfetexte

### 2. Deutsche KI-Antworten
- **Deutsche System-Prompts** für alle KI-Modelle (OpenAI, Anthropic, Google Gemini, OpenRouter, Llama, lokale Modelle)
- **Intelligente Sprachauswahl** basierend auf deiner Konfiguration
- **Konsistente deutsche Antworten** bei allen Anfragen

### 3. Sprachkonfiguration
- **Einfache Sprachauswahl** in der Konfigurationsoberfläche
- **Unterstützte Sprachen**: Deutsch (de) und Englisch (en)
- **Nachträgliche Änderung** über die Optionen-Seite möglich

## 🚀 Installation

1. **Kopiere die Dateien** in dein `custom_components/ai_agent_ha/` Verzeichnis
2. **Starte Home Assistant neu**
3. **Konfiguriere die Integration** über Einstellungen > Geräte & Services > Integration hinzufügen

## ⚙️ Konfiguration

### Erstmalige Einrichtung

1. **KI-Anbieter wählen**: OpenAI, Anthropic, Google Gemini, OpenRouter, Llama oder lokales Modell
2. **Sprache auswählen**: Deutsch oder Englisch
3. **API-Schlüssel eingeben**: Je nach gewähltem Anbieter
4. **Modell auswählen**: Optional ein spezifisches Modell wählen

### Sprache ändern

Du kannst die Sprache jederzeit über die **Optionen-Seite** der Integration ändern:

1. Gehe zu **Einstellungen** > **Geräte & Services**
2. Finde **AI Agent HA** in der Liste
3. Klicke auf **Konfigurieren**
4. Wähle deine gewünschte **Sprache** aus
5. **Speichern**

## 🎯 Deutsche Features im Detail

### System-Prompts
Die KI-Modelle erhalten je nach gewählter Sprache unterschiedliche System-Prompts:

**Deutsch (de):**
- "Du bist ein KI-Assistent, der in Home Assistant integriert ist..."
- Alle Befehle und Anweisungen auf Deutsch
- Deutsche Beispiele und Fehlermeldungen

**Englisch (en):**
- "You are an AI assistant integrated with Home Assistant..."
- Englische Befehle und Anweisungen (ursprüngliche Version)

### Lokalisierte Services
Alle Services sind vollständig übersetzt:

- `ai_agent_ha.query` → "KI-Agent mit Home Assistant Kontext abfragen"
- `ai_agent_ha.create_dashboard` → "Dashboard über KI-Agent erstellen"
- `ai_agent_ha.create_automation` → "Automatisierung über KI-Agent erstellen"
- `ai_agent_ha.update_dashboard` → "Dashboard über KI-Agent aktualisieren"

## 🔧 Technische Details

### Dateien-Struktur
```
custom_components/ai_agent_ha/
├── agent.py              # Erweitert um deutsche System-Prompts
├── config_flow.py        # Sprachauswahl in Konfiguration
├── const.py              # Sprachkonstanten
├── services_de.yaml      # Deutsche Service-Beschreibungen
└── translations/
    ├── de.json           # Verbesserte deutsche Übersetzungen
    └── en.json           # Englische Übersetzungen
```

### Neue Konstanten
```python
CONF_LANGUAGE = "language"
DEFAULT_LANGUAGE = "en"
SUPPORTED_LANGUAGES = ["en", "de"]
```

### System-Prompt Auswahl
Die Integration wählt automatisch den passenden System-Prompt basierend auf:
1. **Anbieter**: Standard vs. Lokal
2. **Sprache**: Deutsch vs. Englisch

## 🎨 Anwendungsbeispiele

### Deutsche Anfragen
```
"Schalte alle Lichter im Wohnzimmer ein"
"Wie ist das Wetter heute?"
"Erstelle eine Automatisierung, die jeden Abend um 22 Uhr alle Lichter ausschaltet"
```

### Deutsche Antworten
Die KI antwortet auf Deutsch mit natürlicher Sprache:
```json
{
  "request_type": "final_response",
  "response": "Ich habe alle Lichter im Wohnzimmer eingeschaltet. Es waren 3 Lichter, die jetzt auf 100% Helligkeit stehen."
}
```

## 🌍 Mehrsprachigkeit erweitern

### Neue Sprache hinzufügen
Um weitere Sprachen hinzuzufügen:

1. **Konstanten erweitern** in `const.py`:
   ```python
   SUPPORTED_LANGUAGES = ["en", "de", "fr"]  # Französisch hinzufügen
   ```

2. **Übersetzung erstellen** in `translations/fr.json`

3. **System-Prompt hinzufügen** in `agent.py`:
   ```python
   SYSTEM_PROMPT_FR = {
       "role": "system",
       "content": "Vous êtes un assistant IA intégré à Home Assistant..."
   }
   ```

4. **Konfiguration erweitern** in `config_flow.py`:
   ```python
   LANGUAGE_OPTIONS = {
       "en": "English",
       "de": "Deutsch", 
       "fr": "Français"
   }
   ```

## 🔄 Migration von der Original-Version

Wenn du bereits die Original-Version verwendest:

1. **Backup erstellen** deiner aktuellen Konfiguration
2. **Dateien ersetzen** mit der multilingualen Version
3. **Home Assistant neustarten**
4. **Sprache konfigurieren** in den Integrationsoptionen

Deine bestehende Konfiguration bleibt erhalten, nur die Sprache wird hinzugefügt (Standard: Englisch).

## 🛠️ Troubleshooting

### KI antwortet auf Englisch trotz deutscher Konfiguration
1. Überprüfe die Spracheinstellung in den Integrationsoptionen
2. Starte Home Assistant nach Sprachänderung neu
3. Prüfe die Logs auf Fehlermeldungen: `grep "AiAgentHaAgent" home-assistant.log`

### Übersetzungen werden nicht angezeigt
1. Lösche den Browser-Cache
2. Lade die Seite neu (Strg+F5)
3. Prüfe, ob die Datei `translations/de.json` korrekt ist

### Fehlende deutsche Service-Beschreibungen
1. Überprüfe, ob `services_de.yaml` existiert
2. Restart Home Assistant Core
3. Gehe zu Entwicklertools > Services und suche nach "ai_agent_ha"

## 📝 Mitwirken

Verbesserungen und weitere Übersetzungen sind willkommen! Die wichtigsten Bereiche:

1. **Übersetzungen verfeinern** in `translations/de.json`
2. **Neue Sprachen hinzufügen**
3. **System-Prompts optimieren** für bessere KI-Antworten
4. **Dokumentation erweitern**

## 📄 Lizenz

Diese Erweiterung behält dieselbe Lizenz wie das Original-Projekt.

---

**Viel Spaß mit deinem deutschen AI Assistant! 🇩🇪🤖**
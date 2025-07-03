# API-Kommunikation Analyse - AI Agent HA

## Aktuelle Implementierung 

### ✅ **Vorhandene Funktionalitäten**

#### 1. **Rate Limiting**
- **Basis-Rate-Limiting**: 60 Anfragen pro Minute (konfiguriert)
- **Zeitfenster-basierte Überwachung**: 60-Sekunden-Fenster
- **Request-Counter**: Zählt Anfragen pro Zeitfenster
- **Location**: `agent.py` Line 1042-1051

```python
def _check_rate_limit(self) -> bool:
    current_time = time.time()
    if current_time - self._request_window_start >= 60:
        self._request_count = 0
        self._request_window_start = current_time
    
    if self._request_count >= self._rate_limit:
        return False
    
    self._request_count += 1
    return True
```

#### 2. **Retry-Mechanismus**
- **Max Retries**: 10 Versuche (konfiguriert)
- **Linear Backoff**: `retry_delay * retry_count` (1, 2, 3, 4... Sekunden)
- **Fehlerbehandlung**: Generische Exception-Behandlung
- **Location**: `agent.py` Line 2531-2572

#### 3. **API-Provider Support**
- ✅ OpenAI (mit spezifischen Modell-Parametern)
- ✅ Gemini
- ✅ Anthropic
- ✅ OpenRouter
- ✅ Local Models

---

## ❌ **Identifizierte Probleme**

### 1. **Unzureichendes Token-Management**

**Problem**: Keine Token-Vorhersage oder -Überwachung
- ❌ Keine Berechnung der Request-Token vor API-Aufrufen
- ❌ Keine Berücksichtigung der OpenAI TPM (Tokens Per Minute) Limits
- ❌ Conversation History wird nur auf 10 Nachrichten begrenzt, aber Token nicht gezählt

**Ihre Fehlermeldung zeigt**:
```
Rate limit reached for gpt-4o-mini: Limit 200000, Used 110210, Requested 103361
```
→ Der Code versucht 103.361 Token anzufordern, obwohl nur 89.790 verfügbar sind

### 2. **Keine Intelligente 429-Behandlung**

**Problem**: Keine spezifische Behandlung von Rate-Limit-Fehlern
- ❌ Kein Parsing der Retry-After Header aus 429-Responses
- ❌ Keine Unterscheidung zwischen TPM/RPM/TMP Limits
- ❌ Linear statt exponential backoff

**OpenAI liefert in 429-Fehlern**:
- `Retry-After` Header mit empfohlener Wartezeit
- Detaillierte Limit-Informationen
- Spezifische Fehlertypen (tokens, requests, etc.)

### 3. **Ineffiziente Conversation History**

**Problem**: Token-ineffiziente Nachrichtenverwaltung
```python
# Line 2536-2540
recent_messages = self.conversation_history[-10:] if len(self.conversation_history) > 10 else self.conversation_history
```
- ❌ Begrenzt nur auf Nachrichtenanzahl, nicht auf Token
- ❌ Lange System-Prompts (>2000 Token) werden bei jedem Call übertragen
- ❌ Keine Komprimierung oder Zusammenfassung alter Nachrichten

### 4. **Fehlende Konfigurierbarkeit**

**Problem**: Hardcoded Limits
```python
self._max_retries = 10           # Hardcoded
self._retry_delay = 1            # Hardcoded
self._rate_limit = 60            # Hardcoded - gilt für alle Provider gleich
```

---

## 🔧 **Empfohlene Verbesserungen**

### 1. **Intelligentes Token-Management**

```python
class TokenManager:
    def __init__(self, model):
        self.model = model
        self.token_limits = {
            "gpt-4o-mini": {"tpm": 200000, "rpm": 10000, "context": 16384},
            "gpt-4o": {"tpm": 30000, "rpm": 500, "context": 8192}
        }
    
    def estimate_tokens(self, messages):
        # Implementierung mit tiktoken für genaue Token-Zählung
        pass
    
    def can_make_request(self, messages):
        estimated = self.estimate_tokens(messages)
        return estimated < self.get_available_tokens()
```

### 2. **Spezifische 429-Error-Behandlung**

```python
async def handle_rate_limit_error(self, error_response):
    if "rate_limit_exceeded" in error_response:
        # Parse retry-after Zeit aus der Antwort
        retry_after = self.parse_retry_after(error_response)
        
        # Intelligente Wartezeit basierend auf Fehlertyp
        if "tokens per min" in error_response:
            wait_time = max(retry_after, 60)  # Mindestens 1 Minute warten
        elif "requests per min" in error_response:
            wait_time = retry_after or 5      # Kurze Wartezeit für RPM
        
        await asyncio.sleep(wait_time)
```

### 3. **Exponential Backoff mit Jitter**

```python
def calculate_backoff(self, retry_count, base_delay=1, max_delay=60):
    # Exponential backoff: 1, 2, 4, 8, 16, 32, 60, 60, ...
    delay = min(base_delay * (2 ** retry_count), max_delay)
    
    # Jitter hinzufügen um Thundering Herd zu vermeiden
    jitter = random.uniform(0.1, 0.3) * delay
    return delay + jitter
```

### 4. **Provider-spezifische Konfiguration**

```python
PROVIDER_CONFIGS = {
    "openai": {
        "rate_limits": {"tpm": 200000, "rpm": 10000},
        "max_retries": 10,
        "base_delay": 1,
        "timeout": 300
    },
    "gemini": {
        "rate_limits": {"rpm": 1500},
        "max_retries": 5,
        "base_delay": 2,
        "timeout": 30
    }
}
```

### 5. **Conversation History Optimierung**

```python
def optimize_conversation_history(self, messages, max_tokens=4000):
    """Optimiere Conversation History basierend auf Token-Limits"""
    
    # System Prompt immer behalten
    system_msg = messages[0] if messages[0]["role"] == "system" else None
    other_messages = messages[1:] if system_msg else messages
    
    # Von hinten nach vorne Token zählen bis Limit erreicht
    selected_messages = []
    current_tokens = self.count_tokens(system_msg) if system_msg else 0
    
    for msg in reversed(other_messages):
        msg_tokens = self.count_tokens(msg)
        if current_tokens + msg_tokens > max_tokens:
            break
        selected_messages.insert(0, msg)
        current_tokens += msg_tokens
    
    return [system_msg] + selected_messages if system_msg else selected_messages
```

---

## 🚨 **Sofortmaßnahmen für Ihr Problem**

### 1. **Reduzierung der Token-Last**

**Conversation History begrenzen**:
```python
# Statt 10 Nachrichten, basierend auf Token begrenzen
max_context_tokens = 8000  # Für gpt-4o-mini
recent_messages = self.optimize_conversation_history(
    self.conversation_history, 
    max_context_tokens
)
```

**System Prompt optimieren**:
- Aktueller System Prompt: ~2000+ Token
- Empfehlung: Kürzere, fokussiertere Prompts für einfache Anfragen

### 2. **Bessere Rate Limit Detection**

```python
def parse_openai_error(self, error_text):
    """Parse OpenAI error für spezifische Behandlung"""
    if "rate_limit_exceeded" in error_text:
        # Extract wait time: "Please try again in 4.071s"
        import re
        wait_match = re.search(r"try again in ([\d.]+)s", error_text)
        if wait_match:
            return float(wait_match.group(1)) + 1  # +1s Buffer
    return None
```

### 3. **Provider-spezifische Token-Limits**

```python
# Für OpenAI gpt-4o-mini:
# TPM: 200,000 tokens/minute
# Context Window: 16,384 tokens

self.model_limits = {
    "gpt-4o-mini": {
        "max_tokens_per_request": 16384,
        "tokens_per_minute": 200000,
        "safe_token_limit": 160000  # 80% vom Limit
    }
}
```

---

## 📊 **Bewertung der aktuellen Implementierung**

| Aspekt | Status | Bewertung |
|--------|--------|-----------|
| **Basic Rate Limiting** | ✅ Vorhanden | 6/10 - Zu einfach |
| **Retry Mechanism** | ✅ Vorhanden | 5/10 - Nicht intelligent |
| **Token Management** | ❌ Fehlt | 2/10 - Kritisches Problem |
| **429-Error Handling** | ❌ Unzureichend | 3/10 - Keine spezifische Behandlung |
| **Provider-Awareness** | ⚠️ Teilweise | 4/10 - Nicht provider-spezifisch |
| **Konfigurierbarkeit** | ❌ Fehlt | 3/10 - Zu viele Hardcoded Werte |

**Gesamtbewertung: 4/10** - Funktional, aber nicht produktionsreif für komplexe Anwendungen

---

## 💡 **Fazit**

Die aktuelle Implementierung bietet **grundlegende** Rate Limiting und Retry-Funktionalität, ist aber **nicht ausreichend** für komplexe oder hochfrequente Anwendungen:

**Hauptprobleme**:
1. **Keine Token-Awareness** → Führt zu Ihrem 429-Fehler
2. **Ineffiziente Conversation History** → Verschwendet Token
3. **Nicht provider-spezifisch** → Suboptimale Performance

**Ihre Fehlermeldung** zeigt ein typisches Token-Management-Problem: Der Code versucht mehr Token anzufordern als verfügbar, ohne dies vorher zu prüfen.

**Empfehlung**: Implementieren Sie zunächst ein einfaches Token-Management und intelligente 429-Behandlung, bevor Sie komplexere Features nutzen.
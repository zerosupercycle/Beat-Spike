# Beat Spike — Polymarket beat-cross momentum bot

ROOT := $(abspath .)
VENV := $(ROOT)/venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
CONFIG ?= config/default.yaml
MONITOR_CONFIG ?= config/monitor.yaml
HOST ?= 0.0.0.0
PORT ?= 8788
WEB_PORT ?= 5174

export PYTHONPATH := $(ROOT)

# PM2 app names (make pm2-start | pm2-stop | pm2-restart | pm2-logs)
PM2_SERVER := beat-spike-server
PM2_BOT := beat-spike-bot
PM2_WEB := beat-spike-web
PM2_MONITOR := beat-spike-monitor
PM2_APPS := $(PM2_SERVER) $(PM2_BOT) $(PM2_WEB) $(PM2_MONITOR)

.PHONY: help install install-server install-web env server server-stop bot bot-stop monitor web web-build clean health
.PHONY: pm2-check pm2-start pm2-stop pm2-restart pm2-reload pm2-delete pm2-status pm2-logs pm2-save

help:
	@echo "Beat Spike — available targets:"
	@echo ""
	@echo "  make install        Bot + server Python deps + web npm"
	@echo "  make server         Dashboard API + feeds on :$(PORT)"
	@echo "  make bot            Run trading bot (CONFIG=$(CONFIG))"
	@echo "  make bot-stop       Stop bot (PM2 + any make bot process)"
	@echo "  make monitor        Profile RTDS monitor (MONITOR_CONFIG=$(MONITOR_CONFIG))"
	@echo "  make web            Vite UI → http://localhost:$(WEB_PORT)"
	@echo ""
	@echo "  PM2 (server + bot + web + monitor in background)"
	@echo "  make pm2-start      Start all three via pm2"
	@echo "  make pm2-stop       Stop all pm2 apps"
	@echo "  make pm2-restart    Restart running pm2 apps (or start if missing)"
	@echo "  make pm2-reload     Delete + start fresh (picks up CONFIG/PORT changes)"
	@echo "  make pm2-status     pm2 status"
	@echo "  make pm2-logs       Tail combined logs"
	@echo "  make pm2-delete     Remove pm2 apps"
	@echo "  make pm2-save       pm2 save (persist across reboot; run pm2 startup once)"
	@echo ""
	@echo "  Variables: CONFIG=… MONITOR_CONFIG=… HOST=… PORT=… WEB_PORT=…"

install: install-server install-web env
	@echo "Install complete. Next: make server | make bot | make web"

install-server:
	@test -x $(PYTHON) || (cd $(ROOT) && python3 -m venv venv)
	$(PIP) install -r $(ROOT)/requirements.txt
	$(PIP) install -r $(ROOT)/server/requirements.txt

install-web:
	cd $(ROOT)/web && npm install

env:
	@if [ ! -f $(ROOT)/.env ]; then \
		cp $(ROOT)/.env.example $(ROOT)/.env; \
		echo "Created .env from .env.example"; \
	else \
		echo ".env already exists"; \
	fi

server-stop:
	@fuser -k $(PORT)/tcp 2>/dev/null && echo "Stopped :$(PORT)" || echo "Nothing on :$(PORT)"

server:
	@test -x $(PYTHON) || (echo "Run make install first" && exit 1)
	@if ss -tln | grep -q ":$(PORT) "; then \
		echo "Port $(PORT) in use. Run: make server-stop"; exit 1; \
	fi
	cd $(ROOT)/server && $(PYTHON) main.py --host $(HOST) --port $(PORT)

bot-stop:
	@command -v pm2 >/dev/null 2>&1 && pm2 describe $(PM2_BOT) >/dev/null 2>&1 && \
		pm2 stop $(PM2_BOT) >/dev/null 2>&1 && echo "Stopped PM2 $(PM2_BOT)" || true
	@ids=$$(pgrep -f "[p]ython -m bot.main --config $(CONFIG)" 2>/dev/null || true); \
	if [ -n "$$ids" ]; then \
		kill $$ids 2>/dev/null && echo "Stopped bot.main (pids $$ids)"; \
	else \
		echo "No bot.main running"; \
	fi

bot:
	@test -x $(PYTHON) || (echo "Run make install first" && exit 1)
	@pid=$$(command -v pm2 >/dev/null 2>&1 && pm2 pid $(PM2_BOT) 2>/dev/null || true); \
	if [ -n "$$pid" ] && [ "$$pid" != "0" ]; then \
		echo "Stopping PM2 $(PM2_BOT) — interactive make bot takes over"; \
		pm2 stop $(PM2_BOT) >/dev/null; \
	fi
	@if pgrep -f "[p]ython -m bot.main --config $(CONFIG)" >/dev/null 2>&1; then \
		echo "Bot already running. Run: make bot-stop"; exit 1; \
	fi
	cd $(ROOT) && $(PYTHON) -m bot.main --config $(CONFIG)

monitor:
	@test -x $(PYTHON) || (echo "Run make install first" && exit 1)
	cd $(ROOT) && $(PYTHON) -m bot.monitor.main --config $(MONITOR_CONFIG)

web:
	cd $(ROOT)/web && npm run dev -- --port $(WEB_PORT)

web-build:
	cd $(ROOT)/web && npm run build

health:
	@curl -sf "http://127.0.0.1:$(PORT)/api/health" | $(PYTHON) -m json.tool 2>/dev/null || echo "Server not running on :$(PORT)"

pm2-check:
	@command -v pm2 >/dev/null 2>&1 || (echo "pm2 not found — install: npm install -g pm2" && exit 1)

pm2-start: pm2-check
	@test -x $(PYTHON) || (echo "Run make install first" && exit 1)
	@pm2 describe $(PM2_SERVER) >/dev/null 2>&1 || \
		(cd $(ROOT)/server && pm2 start main.py --name $(PM2_SERVER) --interpreter $(PYTHON) -- --host $(HOST) --port $(PORT))
	@pm2 describe $(PM2_BOT) >/dev/null 2>&1 || \
		(cd $(ROOT) && env PYTHONPATH=$(ROOT) pm2 start $(PYTHON) --name $(PM2_BOT) -- -m bot.main --config $(CONFIG))
	@pm2 describe $(PM2_WEB) >/dev/null 2>&1 || \
		(cd $(ROOT)/web && pm2 start npm --name $(PM2_WEB) -- run dev -- --port $(WEB_PORT) --host $(HOST))
	@pm2 describe $(PM2_MONITOR) >/dev/null 2>&1 || \
		(cd $(ROOT) && env PYTHONPATH=$(ROOT) pm2 start $(PYTHON) --name $(PM2_MONITOR) -- -m bot.monitor.main --config $(MONITOR_CONFIG))
	@echo "PM2 apps: $(PM2_APPS)"
	@$(MAKE) pm2-status

pm2-stop: pm2-check
	@pm2 stop $(PM2_APPS) 2>/dev/null || true

pm2-restart: pm2-check
	@pm2 restart $(PM2_APPS) 2>/dev/null || $(MAKE) pm2-start

pm2-reload: pm2-check
	@$(MAKE) pm2-delete
	@$(MAKE) pm2-start

pm2-delete: pm2-check
	@pm2 delete $(PM2_APPS) 2>/dev/null || true

pm2-status: pm2-check
	@pm2 status $(PM2_APPS)

pm2-logs: pm2-check
	@pm2 logs $(PM2_APPS) --lines 100

pm2-save: pm2-check
	@pm2 save

clean:
	rm -rf $(ROOT)/venv $(ROOT)/web/node_modules $(ROOT)/web/dist
	find $(ROOT) -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

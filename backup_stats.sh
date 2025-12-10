#!/bin/bash
# Manual trigger for proxy stats backup
# Usage: ./backup_stats.sh [host:port]

HOST="${1:-localhost:6942}"

echo "Triggering stats backup on $HOST..."
response=$(curl -s -X POST "http://$HOST/backup-stats")

echo "$response" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"Status: {d.get('status')}\nPath: {d.get('path', 'N/A')}\nSources: {d.get('sources', 'N/A')}\nProxies: {d.get('total_proxies', 'N/A')}\")" 2>/dev/null || echo "$response"

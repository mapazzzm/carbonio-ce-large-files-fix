#!/bin/bash
# Bumps carbonio-files Java -Xmx from 4 GB to 12 GB. Backs up wrapper first.
WRAP=/usr/bin/carbonio-files
cp -a $WRAP $WRAP.bak.$(date +%Y%m%d_%H%M%S)
sed -i 's/-Xmx4096m/-Xmx12288m/' $WRAP
systemctl restart carbonio-files
sleep 3
chown zextras:zextras /opt/zextras/data/tmp/nginx/client
echo "Java heap bumped. /usr/bin/carbonio-files:"
grep -E "Xmx|Xms" $WRAP

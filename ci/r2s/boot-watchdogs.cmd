# R2S watchdog handoff: Linux's vendor DTB does not take over these timers.
# SPM8821 clamps the advertised 60-second U-Boot timeout to 16 seconds.
if wdt list; then
	for r2s_wdt in PMIC_WDT watchdog@D4080000 watchdog@d4080000; do
		if wdt dev ${r2s_wdt}; then
			if wdt stop; then
				echo "R2S: stopped inherited watchdog ${r2s_wdt}"
			else
				echo "R2S: cannot stop watchdog ${r2s_wdt}; aborting boot"
				exit 1
			fi
		fi
	done
	setenv r2s_wdt
else
	echo "R2S: U-Boot watchdog command unavailable"
fi

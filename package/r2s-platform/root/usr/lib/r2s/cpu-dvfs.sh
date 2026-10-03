#!/bin/sh
# POSIX library: use cpufreq's OPP transitions, never program a voltage directly.
r2s_cpu_read()
{
    r2s_cpu_value=''
    IFS= read -r r2s_cpu_value < "$1/$2" || return 1
    printf '%s\n' "$r2s_cpu_value"
}

r2s_cpu_write()
{
    printf '%s\n' "$3" > "$1/$2"
}

r2s_cpu_policy()
{
    r2s_policy=$1
    [ "$(r2s_cpu_read "$r2s_policy" scaling_driver)" = cpufreq-dt ] || return 1
    r2s_available=" $(r2s_cpu_read "$r2s_policy" scaling_available_frequencies) "
    case "$r2s_available" in *' 614400 '*) ;; *) return 1 ;; esac
    case "$r2s_available" in *' 1600000 '*) ;; *) return 1 ;; esac
    [ "$(r2s_cpu_read "$r2s_policy" scaling_min_freq)" -le 614400 ] || return 1
    [ "$(r2s_cpu_read "$r2s_policy" scaling_max_freq)" -ge 1600000 ] || return 1

    if ! r2s_cpu_write "$r2s_policy" scaling_governor powersave; then return 1; fi
    [ "$(r2s_cpu_read "$r2s_policy" scaling_cur_freq)" = 614400 ] || return 1
    printf 'R2S_DVFS: %s low OPP=614400 kHz\n' "$r2s_policy"
    if ! r2s_cpu_write "$r2s_policy" scaling_governor performance ||
       [ "$(r2s_cpu_read "$r2s_policy" scaling_cur_freq)" != 1600000 ]; then
        r2s_cpu_write "$r2s_policy" scaling_governor powersave || true
        printf 'R2S_DVFS: nominal transition failed; keeping low OPP\n' >&2
        return 1
    fi
    printf 'R2S_DVFS: %s nominal OPP=1600000 kHz verified\n' "$r2s_policy"
}

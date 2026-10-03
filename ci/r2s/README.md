# Debian 13 Router para Orange Pi R2S

## Perfil

El workflow **Build R2S Debian 13 Router** genera una imagen `.img.gz` para
`riscv64` en un runner x86-64 de GitHub, dentro de un contenedor Ubuntu 22.04.
No utiliza `10.0.0.3`. El checkout se monta en `/openwrt`; temporales, fuentes,
cachés, logs y artefactos del build quedan bajo ese bind mount. El contenedor
es efímero y necesita privilegios para `chroot`, Btrfs, namespaces y loop devices.

La construcción es multijob:

```text
prepare ─┬─ kernel ─┐
         ├─ uboot ──┼─ assemble ─ verify ─ publish
         └─ rootfs ─┘
```

`prepare` fija fuentes, toolchain y epoch una sola vez, ejecuta las pruebas Linux
y exporta la imagen OCI exacta mediante `docker save`/Zstd. Los demás jobs cargan
esa misma imagen y comprueban su identidad; no reconstruyen capas APT ni consultan
ramas flotantes. Los tres componentes se ejecutan en paralelo, usando solo el
bundle Git que necesitan. `rootfs` también produce firmware/BSP.

Los artifacts intermedios son archivos tar con manifests de identidad e inventario
SHA-256. Se validan rol, ejecución, commit del builder, fuentes, arquitectura,
contenedor, toolchain y epoch antes de consumirlos. La extracción rechaza rutas
externas, enlaces y entradas duplicadas. Los paquetes y las referencias compiladas
del kernel/DTB/U-Boot se transfieren como resultados obligatorios, no como caché.

`assemble` exige todos los paquetes precompilados y desactiva la limpieza: un
paquete ausente hace fallar la etapa, nunca inicia una compilación implícita.
`verify` vuelve a comprobar los bytes transferidos, descomprime y valida la imagen
en un runner independiente. Solo `publish`, dependiente de `verify`, ofrece la
entrega final con checksums. No se transfiere el árbol completo de objetos de build.

Configuración inicial:

| Función | Valor |
|---|---|
| WAN | Controlador físico `ethernet@cac80000`, correspondiente al `eth0` original |
| Acceso WAN | DHCP IPv4; IPv6 automático en WAN |
| LAN | Puente `br-lan` con los demás puertos Ethernet físicos |
| Dirección LAN | `10.0.0.1/24` |
| DHCP | `10.0.0.100–199`, concesiones de 12 horas |
| DNS y gateway | `10.0.0.1`; nombres internos bajo `home.arpa` |
| Administración | SSH/consola, usuario `admin`, sudo sin contraseña |
| Autenticación SSH | Clave pública; acceso root y contraseñas por SSH deshabilitados |
| DNS | dnsmasq LAN/localhost → dnscrypt-proxy `127.0.0.1:5053` → DoH |
| Upstreams | Cloudflare y Quad9, IPs fijas y hostname TLS autenticado |
| Filtrado | HaGeZi Light y allowlist local; actualización diaria transaccional |
| Docker | Paquetes Debian; driver, rutas y opciones del daemon predeterminados |

NetworkManager mantiene perfiles explícitos; no crea conexiones Ethernet
automáticas que compitan con el puente. La WAN se reconoce por device tree,
no por el orden de enumeración PCIe. Se puede indicar `wan_override` en
`/etc/r2s/router.json` tras verificar el hardware. Si no hay una correspondencia
válida, no se configura un puerto WAN arbitrariamente.

IPv6 LAN admite un **prefijo /64 explícitamente enrutado** mediante
`ipv6_lan_prefix`. Sin ese dato se conserva link-local en LAN y no se anuncian
prefijos globales inventados. La delegación dinámica de prefijos del ISP deberá
configurarse según la conexión; no se presupone que un prefijo WAN sea delegable.

## Ejecutar GitHub Actions

1. Subir los cambios a `orangepi-build`. El workflow debe existir en la rama por
   defecto para mostrar **Run workflow**.
2. Abrir **Actions → Build R2S Debian 13 Router → Run workflow**.
3. Mantener `use_cache=true`. La primera ejecución descarga el toolchain y
   prepara las fuentes y el sistema base.
4. Descargar el artifact `r2s-debian13-<run_id>-<attempt>`.

| Entrada | Valor inicial |
|---|---|
| `use_cache` | `true` |
| `debian_snapshot` | `20260928T000000Z` |
| `kernel_commit` | vacío: resolver y fijar `orange-pi-6.6-ky` |
| `uboot_commit` | vacío: resolver y fijar `v2022.10-ky` |

El artifact contiene imagen, paquetes `.deb`, bootfs, U-Boot, configuración
expandida, locks, manifests, logs y `sha256sums`. Se conserva **14 días**.
Los diagnósticos se publican también si falla una etapa. No se crean releases
automáticas.

Los artifacts intermedios `r2s-RUN-PREPARE_ATTEMPT-{context,environment,kernel,...}`
caducan a los **3 días**. Los logs por etapa y la entrega final duran 14 días.
El nombre del contexto permanece estable al reintentar solamente jobs fallidos;
cada manifest registra además el intento del productor. Se puede usar **Re-run
failed jobs** dentro de esa ventana para reutilizar etapas completas. Reejecutar
todo el workflow crea un contexto nuevo y descubre/fija las fuentes nuevamente.

```bash
sha256sum -c sha256sums
gzip -t Orangepir2s_*.img.gz
```

`raw-image.sha256` nombra el contenido descomprimido como `r2s-debian.img`.
El nombre del fichero incluye `minimal`, referido a la base headless seleccionada;
la imagen sí incorpora el manifiesto completo de capacidades del router.

## Reproducibilidad y cachés

Linux, U-Boot y firmware se fijan por commits completos antes de compilar.
La base OCI y las acciones se fijan por digest/commit; debootstrap 1.0.141 y
el keyring Debian 2025.1 se verifican por SHA-256. El proveedor KY publica solo
MD5 de su toolchain: se comprueba el valor versionado por Orange Pi y el SHA-256
completo fijado en `prepare.py` tras verificar el archivo real. Ese origen histórico
utiliza HTTP; no se desactiva
TLS para las otras fuentes.

El SDK contiene `sysroot/lib/gcc -> /home/ci/gcc_linux_install/lib/gcc`, un enlace
al prefijo del entorno de empaquetado original. `toolchain.py` verifica SHA-256,
valida todas las entradas y lo reubica como `../../lib/gcc` dentro del SDK.
Los enlaces de sysroot a `/lib`/`usr` se interpretan dentro de su sysroot; nunca
se enlazan al host. Se rechazan escapes, tipos especiales, ciclos, hardlinks sin
datos y archivos atravesando enlaces. La extracción se prepara en un directorio
temporal bajo `toolchains` y se publica solo al completarse; no se sobrescribe
un compilador existente. Los errores muestran el miembro y su destino.

Se publican `SOURCE_DATE_EPOCH`, identidad del contenedor y versiones de sus
paquetes. Reconstruir su capa APT puede cambiar dependencias del host y por tanto
su identidad. Los locks permiten identificar el build, pero no se declara una
garantía de reproducción bit a bit sin conservar también ese entorno exacto.

| Caché | Contenido / invalidación |
|---|---|
| Toolchain | Archivo verificado, runner y checksum |
| Git | Objetos bare, commits fijados; fallback para reutilizar objetos |
| `ccache` | Kernel 1792 MiB + U-Boot 256 MiB; claves y escritores independientes |
| Rootfs | Hash de receta de bootstrap, snapshot y entorno; coincidencia exacta, sin fallback |
| Docker Buildx | Capas del contenedor mediante backend `gha` |

Las etapas completas se guardan aunque falle después el ensamblado. El rootfs
se comprueba con LZ4, tar y SHA-256; no se cachean árboles con `.o`, paquetes
generados ni imágenes. GitHub puede expulsar cachés: el arranque en frío sigue
siendo válido. Cambiar el perfil invalida el rootfs; `ccache` comprueba los objetos
contra sus entradas reales. El resumen muestra aciertos, tamaños y duración.

El hash de receta incluye manifiesto de paquetes, perfil, entorno chroot, scripts
del builder y configuraciones que participan en debootstrap. Se guarda y comprueba
también dentro del cache. Una corrección del validador o del paquete de integración
aplicado después del bootstrap no obliga a repetir la instalación de toda la base;
sí se regeneran firmware/BSP, r2s-platform y sus manifests con el commit actual.

Solo `prepare` guarda el cache Git/toolchain; los jobs paralelos no compiten por
una misma clave. Los caches del compilador están separados por componente y el
cache rootfs solo lo escribe `rootfs`. `assemble`, `verify` y `publish` no obtienen
resultados compilados de ejecuciones anteriores desde caches.

## Paquetes y kernel

`packages.list` es el baseline Debian explícito, instalado sin recommends.
Incluye red, firewall, VPN, DNS, Docker/Compose, almacenamiento, diagnóstico,
cron/NTP y estadísticas. El paquete fuente `package/r2s-platform` produce un
`.deb` declarativo con conffiles, unidades systemd e integraciones propias.

El perfil elimina el SDK/cámara/demos de la familia KY, evita instalar
`orangepi-config`/zsh, paquetes de desarrollo y el `.deb` de headers en `/opt`.
Las cabeceras compiladas siguen disponibles entre los paquetes del artifact.
APT no conserva sus `.deb` descargados; la imagen se limpia al terminar.

`kernel.required` aplica y valida el contrato tras `olddefconfig`. Btrfs y ext4
se integran en el kernel; WireGuard, VXLAN, MACVLAN, bridge, VLAN, bonding, VRF,
IPsec, PPPoE, nftables y QoS se verifican como módulos del mismo build. Se
habilitan ingress y `NFT_FIB_INET`; se desactiva DWARF/BTF para limitar disco.
No se mezclan módulos de otra compilación ni se instala un kernel genérico
de Debian para sustituir el soporte hardware KY.

El sistema instalado utiliza los repositorios normales de Trixie y seguridad
para APT; el snapshot es el origen fijado de la construcción, no una congelación
permanente de las actualizaciones del router.

## Btrfs y eMMC de 8 GB

```text
Área de arranque vendor
p1: ext4, /boot, inicio 30 MiB, tamaño 512 MiB
p2: Btrfs, inicio 542 MiB, resto del dispositivo
    @root        /
    @data        /data
    @log         /var/log
    @snapshots   /.snapshots
    @docker      /var/lib/docker
    @containerd  /var/lib/containerd
```

Todos comparten espacio libre, sin cuotas iniciales, con `compress=zstd:3,noatime`.
No se reserva una raíz de 3/8 GiB. La imagen se dimensiona por su contenido y el
margen del builder; la validación exige que no exceda 7.000.000.000 bytes. Al
arrancar, `growpart` amplía solo p2, comprueba que no cambie su inicio y expande
Btrfs al espacio disponible. Se guarda la tabla anterior en `/var/lib/r2s`.

Los drop-ins de Docker/containerd únicamente exigen los mounts habituales y
esperan una restauración pendiente. No cambian `daemon.json`, `data-root`, driver
de almacenamiento ni redes del daemon. El firewall convive con iptables-nft y
`DOCKER-USER`: mantener los defaults de Docker no debe cortar el forwarding LAN.
Los bridges nuevos se detectan mediante netlink y un timer de mantenimiento.

## Diagnóstico del arranque en la placa de 2 GiB

La imagen genera estas opciones en `/boot/orangepiEnv.txt`:

```ini
verbosity=7
console=serial
earlycon=on
extraargs=rootflags=subvol=@root mem=2G ignore_loglevel panic=0
```

El DTB vendor fijado declara dos bancos de 2 GiB, mientras que la placa probada
reporta 2 GiB en U-Boot. `mem=2G` limita la memoria utilizable por Linux si el
U-Boot existente no corrige esos bancos antes del arranque. Los parámetros de
consola muestran el progreso completo por SBI hasta que `ttyS0` toma el relevo,
sin `keep_bootcon` para evitar mensajes duplicados sobre la misma UART.
Son parámetros de diagnóstico para esta placa;
no confirman por sí solos compatibilidad con su OpenSBI/U-Boot ni un arranque
correcto. El job `verify` comprueba las opciones dentro de la imagen final.

Capturar al menos 90 segundos de salida serie, incluidos reinicios automáticos.
Para retirar posteriormente la salida detallada, cambiar `verbosity=1` y quitar
`ignore_loglevel` de `extraargs`, conservando el subvolumen y el límite
de memoria hasta validar el DTB efectivo en hardware.

### Inicialización DVFS y arranque normal

La candidata arranca con systemd y target `multi-user.target`. El kernel usa
`CONFIG_CPU_FREQ_DEFAULT_GOV_POWERSAVE=y`; antes del init principal, el paquete
`r2s-platform` incluye en initramfs una inicialización POSIX de cpufreq:

```text
R2S_DVFS: ... low OPP=614400 kHz
R2S_DVFS: ... nominal OPP=1600000 kHz verified
```

La secuencia reproduce la transición validada manualmente en el R2S: 614.4 MHz
con 950 mV declarados por el OPP y regreso a 1.6 GHz con 1050 mV. No programa
voltajes manualmente ni deja un límite permanente a 614.4 MHz. Si falla la subida,
intenta mantener el governor bajo y deja un mensaje explícito en consola.
`verify` comprueba los bytes de la implementación dentro de rootfs y `uInitrd`,
además de las opciones del kernel. La integración automática requiere validar
arranques fríos en la placa; no equivale a una certificación de estabilidad larga.

La red espera sus cuatro puertos físicos, sin bloquearse por `udev-settle` global.
Se deshabilitan ZRAM/ramlog históricos que interfieren con los subvolúmenes Btrfs.
dnsmasq conserva el grupo nativo de su usuario y escribe su pidfile en `/run/r2s`,
una ruta permitida por el confinamiento. Los tests de imagen arrancan sus binarios
reales en namespaces aislados y prueban DHCP, DNS interno, upstream, blocklist y
nftsets; también prueban DoH con el usuario sin privilegios y las fuentes NTP IP.
QEMU user no traduce `NETLINK_NETFILTER`: DHCP/DNS se prueban con el binario
RISC-V real y la inserción nftset con un chroot amd64 del mismo snapshot, exigiendo
la misma versión exacta de dnsmasq. La inserción desde el binario RISC-V requiere
además la prueba física en el R2S.

### Shell temporal como PID 1 para diagnóstico

Para diagnóstico, añadir `init=/bin/sh` a `extraargs`: el initramfs sigue cargando los drivers y
montando el rootfs Btrfs, pero entrega PID 1 a una shell root por la consola serie
en lugar de iniciar systemd. Los servicios de router, DHCP, DNS, SSH y Docker no
se arrancan automáticamente en este modo. `panic=0` evita el reinicio automático
del kernel ante un panic; no impide un reset del hardware o del firmware.
`verify` comprueba estos parámetros y que `/bin/sh` sea ejecutable.

Cuando aparezca el prompt, comprobar si permanece estable al menos 90 segundos:

```sh
cat /proc/cmdline
cat /proc/uptime
sleep 90
cat /proc/uptime
```

No salir de la shell PID 1 con `exit`. Para probar la transición a systemd desde
una shell estable, ejecutar `exec /sbin/init` y capturar la salida. Si el reset
ocurre también antes de iniciar systemd, habrá que investigar el camino de
handoff, los drivers, OpenSBI y la causa de reset del hardware. Si ocurre solo
después del `exec`, habrá que aislar la inicialización de systemd y los servicios.

Para restaurar el arranque normal, retirar `init=/bin/sh panic=0` de `extraargs`
en `/boot/orangepiEnv.txt`. Con init normal se aplican las instrucciones de acceso
y servicios descritas a continuación.

### Imagen instrumentada: strace y syscalls del kernel

La imagen incluye `strace`, `CONFIG_FTRACE_SYSCALLS=y`, tracepoints, tracefs y
`/usr/sbin/r2s-debug` del paquete `r2s-platform`. Desde la shell de diagnóstico:

```sh
r2s-debug info
r2s-debug python
r2s-debug python-init
```

Los dos escenarios Python siguen tanto la carga del programa como sus syscalls.
El proceso se detiene antes de exec hasta instalar el filtro del kernel. La
salida de `strace` y una instancia aislada de tracefs se envían a la consola
serie y a `/data/r2s-debug/<escenario>.XXXXXX/`, junto con kernel, cmdline y uptime.
El helper monta `@data` exclusivamente desde la misma partición Btrfs que la
raíz `@root`; nunca selecciona la p6 histórica de la eMMC. Los mounts de datos
para diagnóstico usan escrituras síncronas para conservar registros ante reset.

Para seguir el salto real a systemd como PID 1:

```sh
exec r2s-debug systemd
```

Este modo rechaza ejecutarse como proceso hijo. Arranca el lector de tracefs y
adjunta `strace` antes del exec, conservando PID 1. Si no puede preparar o adjuntar
el tracer, vuelve a una shell PID 1. El lector queda fuera del filtro para evitar
realimentar sus propias escrituras. La captura serie debe estar activa antes de
ejecutar cualquier escenario: un reset abrupto puede perder los últimos mensajes
que aún estén pendientes de transmisión o escritura.

`verify` comprueba el contrato de tracing del kernel, la instalación de strace,
la sintaxis/propietario/ejecución de `r2s-debug --help`, además del firmware y
bootfs. Los escenarios se ejecutan manualmente en la placa; no se activa tracing
automáticamente ni se arranca un programa que pueda reiniciar la placa al boot.

### Firmware RCPU y watchdogs heredados

`r2s-platform` instala el `esos.elf` versionado del BSP KY y un hook estricto de
initramfs. El driver `CONFIG_X1_REMOTEPROC=y` intenta cargarlo antes de montar la
raíz; instalarlo solo en rootfs no basta. `verify` comprueba su propietario Debian
y compara los bytes del firmware en BSP, rootfs y el `uInitrd` real de `/boot`.

Antes de `booti`, `boot-watchdogs.cmd` detiene los watchdogs heredados de U-Boot:
`PMIC_WDT` y el SoC `watchdog@D4080000` (también acepta el nombre en minúsculas).
El driver SPM8821 del bootloader limita el timeout real a 16 segundos aunque el
banner anuncie 60. El DTB vendor deshabilita el watchdog del SoC y no proporciona
un driver para alimentar el del PMIC. Un error al detener un dispositivo detectado
aborta el script en vez de continuar con un temporizador activo. No se modifica
el entorno persistente del bootloader. Si el bootloader no dispone de `wdt`, se
muestra ese estado en consola. `verify` valida tanto el script como los CRC y el
contenido de su `boot.scr` compilado. La comprobación definitiva del fin del bucle
de reinicios requiere volver a arrancar la nueva imagen en la placa.

## Primer acceso

Después de grabar la imagen y antes de arrancar, colocar una **clave pública** en
la partición ext4 de boot:

```text
r2s-firstboot/authorized_keys
```

No incluir claves privadas ni contraseñas. El primer arranque crea las claves
SSH del host e importa la pública una sola vez para `admin`. Si el archivo no
está presente, el router arranca pero no proporciona una contraseña SSH de fábrica.
Si falta la clave, apagar, añadir el archivo al medio de arranque y volver a
arrancar: no hace falta una contraseña de fábrica. Una vez autenticado, los
comandos de administración son:

```bash
sudo r2sctl firstboot
sudo systemctl restart ssh
ssh admin@10.0.0.1
```

Los puertos SSH y DNS del router se admiten desde LAN; las conexiones nuevas
desde WAN no habilitan administración. DNS 53 de LAN se redirige al resolver
local y se bloquea su salida directa por 53/853. El control de DoH de aplicaciones
sobre 443 requiere políticas específicas; no se afirma bloquear todo canal DNS.
Docker conserva su comportamiento DNS predeterminado.

## Configurar capacidades

```bash
sudo r2sctl status
sudo r2sctl check
sudo r2sctl network          # Reaplicar perfiles explícitos; puede cambiar conectividad
sudo r2sctl refresh          # Routing, nftsets y firewall
sudo r2sctl update-blocklist
sudo r2sctl sqm
sudo r2sctl snapshot
```

Conffiles bajo `/etc/r2s`:

- `router.json`: LAN, DHCP, selector WAN, WANs adicionales, prefijo IPv6 y feed DNS.
- `policies.json`: grupos PBR, origen/dominio, interfaz/gateway y `vpn_only`.
- `sqm.json`: interfaces y velocidades reales de subida/bajada en kbit/s.
- `allowlist.txt`: un dominio por línea, resuelto por la salida cifrada.
- `doh.toml`: endpoints cifrados, configurables y sin exponer el backend a LAN.

Ejemplo de política (la imagen empieza con grupos vacíos):

```json
{"groups":[{"id":1,"interface":"wg0","sources":["10.0.0.20/32"],"domains":[],"vpn_only":true}]}
```

Las tablas 30001–30099 y prioridades 10001–10099 están reservadas para R2S.
Una política exclusiva de VPN instala una ruta unreachable de respaldo y un
killswitch antes de aceptar conexiones establecidas. Los conjuntos DNS se
conservan al recargar nuestro firewall; no se hace `flush ruleset`.

Ejemplo de SQM, sustituyendo las velocidades por las de tu conexión:

```json
{"interfaces":[{"interface":"eth0","upload_kbit":20000,"download_kbit":100000,"enabled":true}]}
```

CAKE actúa sobre subida e IFB sobre bajada. VPN/SQM no se activan con velocidades
inventadas o credenciales embebidas. WANs adicionales usan perfiles DHCP con
métricas y probes TCP; el failover de IPv4 supervisa solo sus rutas DHCP.

Journald se limita a 64 MiB; collectd/RRD conserva series de tamaño fijo bajo
`/data/metrics`, junto con vnstat. `r2sctl snapshot` crea un snapshot read-only de
la raíz y un archivo/checksum de `/boot` en `/data/.boot-snapshots`. Para rollback
se restauran ambos desde un medio de recuperación, manteniendo kernel/módulos
compatibles. No hay rollback o acumulación automática de snapshots.

## Instalar o actualizar en eMMC

El instalador requiere arranque desde USB/medio extraíble, `/data` montado y un
destino eMMC explícito sin particiones montadas. No selecciona un disco por defecto.
Usar el SHA-256 publicado del archivo `.img.gz`:

```bash
sudo install-to-emmc --device /dev/mmcblk0 \
  --image /data/Orangepir2s_VERSION.img.gz --sha256 HASH_COMPLETO \
  --reuse-boot0 --fresh-data
```

Son obligatorias ambas elecciones:

- `--reuse-boot0` o `--update-boot0`.
- `--fresh-data` o `--preserve-data`.

`fresh-data` instala la imagen, verifica lo escrito y genera UUIDs únicos para
no confundir la eMMC con el USB de origen. Borra el layout/datos anteriores; no
convierte una p6 de OpenWrt automáticamente. `preserve-data` exige este layout
Debian compatible: prepara una nueva raíz, conserva conffiles, SSH y todos los
subvolúmenes de datos, verifica backup de bootfs y hace un intercambio atómico
de las raíces con `renameat2`. Los errores de proceso intentan restaurar raíz,
bootfs y áreas de arranque; una interrupción de energía requiere recuperación
desde los backups, no se declara una transacción atómica entre ambos filesystems.

Antes de escribir se verifican backups de cabecera/tabla, cola GPT y boot0 bajo
`/data/.install/FECHA`. Los binarios de arranque provienen del paquete de la imagen
verificada. Se conservan los backups y el estado éxito/fallo. Una instalación
preservada registra paquetes adicionales para restaurarlos por APT antes de
Docker, excluyendo kernels ajenos, metadatos OpenWrt y Adblock.

## Validación

Checks locales:

```bash
python3 -B -m unittest discover -s ci/r2s -p 'test_*.py' -v
for script in ci/r2s/*.sh ci/r2s/config.conf; do bash -n "$script"; done
actionlint .github/workflows/build-r2s-debian.yml .github/workflows/r2s-stage.yml
```

Actions ejecuta primero pruebas Linux aisladas: Btrfs, intercambio/crecimiento
con datos persistentes, forwarding/NAT, convivencia con FORWARD DROP de Docker,
intercepción DNS y killswitch con túnel ausente. Después valida GPT/MBR real,
filesystems, subvolúmenes/fstab, configuración y unidades, paquetes, ausencia de
secretos/desarrollo/cachés, ABI, kernel/DTB y hashes de los componentes de arranque.
Solo publica la imagen cuando pasa la verificación y el gzip corresponde a sus
bytes raw validados.

Los nombres de cadena nftables se generan como identificadores simples con
prefijo `r2s_`: `r2s_input`, `r2s_forward`, `r2s_mark`, `r2s_masquerade`, etc.
La sintaxis de comandos del parser no admite strings entre comillas en esa
posición y los nombres sin prefijo `mark`/`masquerade` son palabras reservadas.
Las palabras de hook/expresión se conservan. La regresión se comprueba en tests
locales y el parser nft real se ejecuta en `prepare` antes de compilar componentes.

Las pruebas locales en macOS no ejecutan mounts/iptables ni una compilación
RISC-V completa. La primera ejecución de Actions y la validación en R2S siguen
siendo necesarias: arranque USB, correspondencia de conectores, eMMC/boot0,
crecimiento, DHCP/DNS, IPv6 según ISP, VPN, QoS y contenedores.

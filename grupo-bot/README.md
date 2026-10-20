# grupo-bot: plantilla de bot de grupo de WhatsApp

> **NÚMERO DESECHABLE, jamás el personal.** Plantilla para armar un bot de
> grupo con Baileys. El punto de partida es mínimo a propósito: vincular el
> número, descubrir JIDs y mandar un "hola".

**Necesita Node 22.9+** (`npm start` usa `--env-file-if-exists`).

## Instalar

```sh
cd grupo-bot
npm install
```

## 1) Vincular el número desechable

```sh
cp .env.example .env   # llena WA_PHONE
npm start
```

- `WA_PHONE` = número desechable, solo dígitos con lada país, sin `+`, espacios
  ni guiones. OJO México: el JID lleva `521` + 10 dígitos, no `52`.
- Sale un **código de vinculación** en la consola: mételo en WhatsApp del
  número desechable (Dispositivos vinculados -> Vincular con número de
  teléfono). Alternativa: `WA_PAIR_MODE=qr` para vincular por QR en terminal.
- Las credenciales quedan en `./auth-*/` (gitignored). Si ves un cierre 401,
  borra el directorio auth y vuelve a vincular.

## 2) Descubrir el JID del grupo

Con la sesión ya vinculada, corre **sin** `GRUPO_JID` (vacío en `.env`):

```sh
npm start
```

Imprime la lista de grupos con su JID. Copia el `...@g.us` del grupo objetivo
a tu `.env`.

## 3) Mandar el primer "hola"

```sh
npm start   # con GRUPO_JID ya puesto
```

Manda **un** "hola" al grupo (una sola vez por proceso) y se queda vivo
escuchando con reconexión automática.

`GRUPO_JID` acepta una lista separada por comas
(`GRUPO_JID=aaa@g.us,bbb@g.us`): el script manda el "hola" a cada grupo con
una pausa de 3 segundos entre envíos.

## Notas

- Reconexión robusta ya incluida (backoff exponencial; solo 401/loggedOut es
  irrecuperable). `markOnlineOnConnect:false` para no robarle notificaciones
  al teléfono.
- Una sola sesión por cuenta: no corras esto en dos máquinas a la vez.
- Comandos, onboarding y publicación de contenido quedan fuera de la
  plantilla a propósito.

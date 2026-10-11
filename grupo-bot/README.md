# grupo-bot — bot de grupo de WhatsApp

> **NUMERO DESECHABLE, jamas el personal.** Objetivo Fase 1: "primer hola".

## Instalar

```sh
cd grupo-bot
npm install
```

## 1) Vincular el numero desechable

```sh
cp .env.example .env   # llena WA_PHONE
npm start
```

- `WA_PHONE` = numero desechable, solo digitos con lada pais, sin `+`, espacios
  ni guiones. OJO Mexico: el JID lleva `521` + 10 digitos, no `52`.
- Sale un **codigo de vinculacion** en la consola: metelo en WhatsApp del
  numero desechable (Dispositivos vinculados -> Vincular con numero de
  telefono). Alternativa: `WA_PAIR_MODE=qr` para vincular por QR en terminal.
- Las credenciales quedan en `./auth-*/` (gitignored). Si ves un cierre 401,
  borra el directorio auth y vuelve a vincular.

## 2) Descubrir el JID del grupo

Con la sesion ya vinculada, corre **sin** `GRUPO_JID` (vacio en `.env`):

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
escuchando con reconexion automatica. Eso es el "done" de Fase 1.

## Notas

- Reconexion robusta ya incluida (backoff exponencial; solo 401/loggedOut es
  irrecuperable). `markOnlineOnConnect:false` para no robarle notificaciones
  al telefono.
- Una sola sesion por cuenta: no corras esto en dos maquinas a la vez.
- Comandos, onboarding y publicacion de contenido son Fase 2 — no estan aqui
  a proposito.

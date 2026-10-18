# CP API Reference

Endpoints used by `cp.py`, reverse-engineered from the cp.pt website. Nothing here is official; CP can change any of it.

Base URLs:

- `API` = `https://api-gateway.cp.pt/cp/services`
- `OIDC` = `https://login.cp.pt/realms/cpclients/protocol/openid-connect`

## Common headers

| Header | Value | Needed by |
| --- | --- | --- |
| `x-cp-connect-id` | `xcck` from `fe-config.json` | every `API` call |
| `x-cp-connect-secret` | `xccs` from `fe-config.json` | every `API` call |
| `X-Api-Key` | `ticketingApiKey` from `fe-config.json` | every `API` call |
| `Origin` / `Referer` | `https://cp.pt` / `https://cp.pt/` | every `API` call |
| `x-access-token` | OIDC `access_token` (expires after 300 s) | ticketing calls |
| `x-cp-client-id` | account email | ticketing calls |

## Configuration & authentication

| Method | Endpoint | Input | Output |
| --- | --- | --- | --- |
| GET | `https://cp.pt/fe-config.json` | none | JSON with `xcck`, `xccs`, `ticketingApiKey` |
| GET | `OIDC/auth` | Query: `client_id=websitecp`, `redirect_uri=https://cp.pt/en/login-check`, `response_type=code`, `response_mode=fragment`, `scope=openid` | Keycloak HTML login page; the form `#kc-form-login` has the action URL |
| POST | form action URL (from the previous step) | Form data: `username`, `password`, `credentialId=""` (don't follow redirects) | **302/303** whose `Location` has `code` in the URL **fragment**. Bad credentials return **200** with the form again |
| POST | `OIDC/token` | Form (`application/x-www-form-urlencoded`): `grant_type=authorization_code`, `client_id`, `redirect_uri`, `code` | JSON: `access_token`, `refresh_token`, `expires_in` (300) |
| POST | `OIDC/token` | Form: `grant_type=refresh_token`, `client_id`, `refresh_token` (no `redirect_uri`) | Same as above; the refresh token is rotated |

## Route & train search

| Method | Endpoint | Input | Output |
| --- | --- | --- | --- |
| GET | `API/travel-api/stations` | none | JSON array of stations: `code`, `designation` |
| POST | `API/travel-api/journeys` | JSON: `departureStationCode`, `arrivalStationCode`, `travelDate` (`YYYY-MM-DD`), `returnDate: null`, `classes: [2]`, `configID: 200`, `lang: "PT"`, `quantities: [{quantity: 1, type: 1}]`, `searchType: 3`, `services: ["IC"]`, `timeLimit` / `returnTimeLimit` (`{startTime: "00:00", endTime: "23:59", limitType: 0}`), `saleableOnly: false`, `username: "sivNetticket"` | JSON `outwardTrip[]`: `departureTime`, `arrivalTime`, `travelSections[0].trainNumber`, ... |
| GET | `https://www.infraestruturasdeportugal.pt/negocios-e-servicos/horarios-ncombio/{train_num}/{date}` | Path: train number, `YYYY-MM-DD`. Send a browser `User-Agent` and `Referer: .../horarios` | JSON `response.DataHoraOrigem`: departure from the **first** station, `DD/MM/YYYY HH:MM:SS` |

## Reservation & checkout

All of these need the authenticated headers.

| Method | Endpoint | Input | Output |
| --- | --- | --- | --- |
| POST | `API/ticketing-api/sale` | JSON: `quantity: 1`, `travelClass: {code: "2"}`, `travelDate`, `outwardTrip: [{trainNumber, departureStation: {code}, arrivalStation: {code}, serviceCode: {code: "IC", designation: "Intercidades"}}]`, `lang: "pt"` | JSON `saleID`, `status.code: "PENDING"`, and the assigned seat in `travelData.outwardTrip[0].seatData`. Locks a seat for **15 minutes**. A full train returns **HTTP 500** with error `WS:RES:116` (see [Error responses](#error-responses)) |
| GET | `API/ticketing-api/train-seats/{sale_id}/trains/{train_num}` | none | Seat map, see [Seat map format](#seat-map-format) |
| PUT | `API/ticketing-api/train-seats/{sale_id}` | JSON: `originalSeats: [{carriageNumber, seatNumber, trainNumber}]`, `requestedSeats: [same shape]` | 200 OK |
| PUT | `API/ticketing-api/sale/{sale_id}/passengers` | JSON: `salePassengers: [{idtype: {code: "CC", designation: "Cartão de Cidadão"}, passengerID, passengerName}]` | 200 OK |
| PUT | `API/ticketing-api/sale/{sale_id}/items` | JSON: `requestedItems: [{itemCode: "302", relatedTrain: null, ticketIndex: 0, type: "DISCOUNT", inputData: <Green Pass number>}]` | 200 OK. Only accepted within 24 h of the train leaving its first station |
| PUT | `API/ticketing-api/sale/{sale_id}/client` | JSON: `clientEmail`, `clientID` (email), `clientMobile`, `clientName` | 200 OK |
| PUT | `API/ticketing-api/sale/{sale_id}/confirm` | Empty body (`Content-Length: 0`) | JSON `status.code == "CONFIRMED"`; the ticket PDF is at `https://cp.pt` + `travelData.ticketData[0].ticketURL` (note: nested in `travelData`) |

## Error responses

CP's errors are JSON with its own error code. Business errors can come back as **HTTP 500**, so check `error` before treating a 500 as a temporary server problem.

```json
{
  "status": 500,
  "error": "WS:RES:116",
  "description": "Atenção\nNão há lugares disponíveis para a totalidade do pedido\nWS:RES:9XX|116",
  "message": "Não foi possível reservar os lugares"
}
```

| `error` | Meaning | Returned by | How `cp.py` treats it |
| --- | --- | --- | --- |
| `WS:RES:116` | No seats available for the whole request (train full) | `POST sale` | Not retryable: a watch reports "no seat"; a scheduled booking keeps trying every ~30 s until 5 min past the unlock mark |

## Seat map format

`GET train-seats/{sale_id}/trains/{train_num}` (abridged):

```json
{
  "trainNumber": 545,
  "orientationCode": 0,
  "carriages": [
    {
      "number": 21,
      "attributes": ["WC", "WIFI", "AC", "PLUGS", "BIKE", "MOBILITY"],
      "occupancyRate": 23,
      "rows": [
        {"number": 0, "places": [
          {"placeType": 1, "seatNumber": 112, "rowCode": 11, "plugType": 0, "withTable": false, "statusCode": 1, "changeable": true},
          {"placeType": 1, "seatNumber": 102, "rowCode": 31, "plugType": 0, "withTable": true, "statusCode": 0, "changeable": true},
          {"placeType": 4},
          {"placeType": 2, "seatNumber": 101, "rowCode": 41, "plugType": 0, "withTable": true, "statusCode": 0, "changeable": true},
          {"placeType": 0},
          {"placeType": 9, "seatNumber": 12, "rowCode": 211, "plugType": 0, "withTable": false, "statusCode": 3, "changeable": false}
        ]},
        {"number": 1, "places": ["..."]},
        {"number": 2, "places": [{"placeType": 0}, {"placeType": 0}, "..."]},
        {"number": 3, "places": [
          {"placeType": 1, "seatNumber": 114, "rowCode": 13, "plugType": 0, "withTable": false, "statusCode": 2, "changeable": true},
          "..."
        ]},
        {"number": 4, "places": ["..."]}
      ]
    }
  ]
}
```

Layout: each car has 5 `rows` that run **along** the car. Rows 0 and 4 are the window seats, rows 1 and 3 the aisle-side seats, and row 2 is the aisle (only `placeType: 0`). Position `i` of every row is the same cross-section of the car. Seat numbers repeat in every car, so a seat is identified by (car, seat).

| Field | Values |
| --- | --- |
| `statusCode` | `0` free, `1` taken, `2` **this sale's seat**, `3` blocked (not sellable, `changeable: false`) |
| `placeType` | `1` / `2` seats (probably facing opposite directions), `0` aisle or empty space, `4` / `5` the two halves of a table, `7` / `9` special places (always blocked in the samples) |
| `withTable` | seat is at a table |
| `plugType` | `1` has a power plug |
| `occupancyRate` | % of the car that is occupied |

## Example: sale created (`POST sale`)

Abridged; identifiers replaced with placeholders.

```json
{
  "saleID": 100000001,
  "reference": "CP-XXXXXXXXXXXX",
  "operation": "SALE",
  "status": {"code": "PENDING", "designation": "Pendente"},
  "totalAmount": "€ 14,70",
  "clientData": {"clientID": "<email>"},
  "travelData": {
    "departure": {"code": "94-31039", "designation": "Lisboa Oriente"},
    "arrival": {"code": "94-34009", "designation": "Entroncamento"},
    "travelDate": "2026-10-19T18:39:00.000+01:00",
    "outwardTrip": [{
      "trainNumber": 515,
      "service": {"code": "IC", "designation": "Intercidades"},
      "departureTime": "18:39",
      "arrivalTime": "19:32",
      "seatData": [{"trainNumber": 515, "carriageNumber": 22, "seatNumber": 114, "relatedItemIndex": 0}]
    }],
    "ticketData": [{
      "itemIndex": 0,
      "code": {"code": "1748", "designation": "Bilhete Longo Curso"},
      "itemType": "TICKET",
      "itemData": {"amount": "€ 14,70"},
      "status": {"code": "PENDING", "designation": "Pendente"}
    }]
  }
}
```

The price is the full fare until the Green Pass discount is applied.

## Example: purchase confirmed (`PUT .../confirm`)

Abridged; personal data and ticket identifiers replaced with placeholders.

```json
{
  "saleID": 100000002,
  "reference": "CP-XXXXXXXXXXXX",
  "status": {"code": "CONFIRMED", "designation": "Confirmado"},
  "totalAmount": "€ 0,00",
  "payment": {"amount": 0, "paymentID": "<payment id>", "paymentType": "PASS"},
  "clientData": {"clientEmail": "<email>", "clientID": "<email>", "clientMobile": "<phone>", "clientName": "", "crmClientID": "<id>"},
  "fiscalData": {"fiscalID": "999999990", "fiscalName": "Consumidor Final"},
  "travelData": {
    "outwardTrip": [{
      "trainNumber": 528,
      "seatData": [{"trainNumber": 528, "carriageNumber": 25, "seatNumber": 87}]
    }],
    "ticketData": [{
      "itemType": "TICKET",
      "ticketID": "<ticket id>",
      "status": {"code": "CONFIRMED", "designation": "Confirmado"},
      "passenger": {
        "passengerName": "<name>",
        "passengerID": "<citizen card number>",
        "idtype": {"code": "CC", "designation": "Cartão de Cidadão"}
      },
      "items": [{
        "itemType": "DISCOUNT",
        "configCode": "302",
        "code": {"code": "4660", "designation": "Passe Ferrov. Verde"},
        "itemData": {"itemInfo": ["<green pass number>"], "amount": "€ 0,00"}
      }],
      "constraints": [{"code": "NON_CHANGEABLE", "designation": "Não Revalidável"}],
      "itemData": {"documentNumber": "<document number>", "invoice": {"invoiceNumber": "<invoice number>", "amount": "€ 0,00"}},
      "barcode": "<barcode>",
      "ticketURL": "/e-ticket/<saleID>/<reference>/<ticketID>"
    }]
  }
}
```

The `ticketURL` (and the `reference` / `barcode`) gives access to the ticket, so treat them as secrets.

## Still unknown

- What `PUT .../confirm` returns when the sale is **already confirmed**. The code treats a non-retryable error after a timed-out confirm as "purchase status unknown" and stops.
- What the `passengers`, `items` and `client` endpoints return after the 15-minute lock has expired.

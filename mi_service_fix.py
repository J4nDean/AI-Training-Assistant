
import os
import json
import logging
import hashlib
import random
import string
import aiohttp
from miservice import MiAccount as BaseMiAccount

class XiaomiLoginError(Exception):
    def __init__(self, code, description):
        self.code = code
        self.description = description
        super().__init__(f"Xiaomi Login Error {code}: {description}")

class MiAccount(BaseMiAccount):
    def __init__(self, username, password, token_path):
        # Inicjalizujemy bez sesji, obsłużymy ją wewnętrznie
        super().__init__(None, username, password, token_path)
        self._session = None

    async def _get_session(self):
        """Zwraca lub tworzy trwałą sesję, która zachowuje ciasteczka."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=False),
                cookie_jar=aiohttp.CookieJar(unsafe=True)
            )
        return self._session

    async def close(self):
        """Zamyka sesję po zakończeniu pracy."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def _serviceLogin(self, uri, data=None):
        """Nadpisujemy _serviceLogin, aby używać TRWAŁEJ sesji."""
        session = await self._get_session()
        
        # Nowsze nagłówki i ciasteczka
        headers = {
            'User-Agent': 'APP/com.xiaomi.mihome APPV/7.13.200 iosPassportSDK/4.2.8 iOS/16.0 miHSTS',
            'Content-Type': 'application/x-www-form-urlencoded' if data else 'application/json'
        }
        
        cookies = {
            'sdkVersion': '4.2',
            'deviceId': self.token['deviceId']
        }
        
        url = 'https://account.xiaomi.com/pass/' + uri
        
        try:
            method = 'POST' if data else 'GET'
            async with session.request(method, url, data=data, cookies=cookies, headers=headers) as r:
                raw = await r.read()
                if not raw:
                    raise Exception("Pusta odpowiedź z serwera Xiaomi")
                
                # Odpowiedzi Xiaomi zaczynają się od &&&START&&&
                if raw.startswith(b'&&&START&&&'):
                    resp = json.loads(raw[11:])
                else:
                    resp = json.loads(raw)
                return resp
        except Exception as e:
            logging.error(f"Błąd sieciowy _serviceLogin: {e}")
            raise

    async def login(self, sid):
        """Główna logika logowania z obsługą Risk Control."""
        if not self.token:
            self.token = await self.token_store.load_token()
            
        if not self.token or 'deviceId' not in self.token:
            # Generujemy standardowy 16-znakowy DeviceID (hex)
            device_id = ''.join(random.choices("0123456789ABCDEF", k=16))
            self.token = {'deviceId': device_id}

        try:
            logging.info(f"Logowanie Xiaomi (Użytkownik: {self.username}, Usługa: {sid}, DeviceID: {self.token['deviceId']})")
            
            # Krok 1: Rozpoczęcie logowania
            resp = await self._serviceLogin(f'serviceLogin?sid={sid}&_json=true&_locale=pl_PL')
            
            # Jeśli 70016 wystąpi na samym początku, spróbujmy z NOWYM DeviceID
            if resp.get('code') == 70016:
                logging.warning("Błąd 70016 wykryty. Próba z rotacją identyfikatora urządzenia...")
                self.token['deviceId'] = ''.join(random.choices("0123456789ABCDEF", k=16))
                resp = await self._serviceLogin(f'serviceLogin?sid={sid}&_json=true&_locale=pl_PL')

            # Obsługa błędu 21315 (nieprawidłowy SID dla Mi Fitness)
            if resp.get('code') == 21315 and sid != 'xiaomiio':
                logging.warning(f"SID '{sid}' odrzucony (21315). Próba przez 'xiaomiio'...")
                resp = await self._serviceLogin('serviceLogin?sid=xiaomiio&_json=true&_locale=pl_PL')

            if resp.get('code') != 0:
                if resp.get('code') == 70016:
                    raise XiaomiLoginError(70016, "Błąd 70016: Xiaomi blokuje połączenie (Risk Control).\n\nMożliwe przyczyny:\n1. Zbyt wiele prób logowania (poczekaj 1h).\n2. IP Twojego serwera jest zablokowane.\n3. Twoje hasło w .env jest błędne.\n\nUpewnij się, że używasz numerycznego 'Xiaomi ID' i poprawnego hasła.")
                raise XiaomiLoginError(resp.get('code'), f"Błąd etapu 1: {resp.get('description')}")
                
            # Krok 2: Przesłanie hasła
            logging.info("Weryfikacja hasła...")
            auth_data = {
                '_json': 'true',
                'qs': resp['qs'],
                'sid': resp['sid'],
                '_sign': resp['_sign'],
                'callback': resp['callback'],
                'user': self.username,
                'hash': hashlib.md5(self.password.encode()).hexdigest().upper()
            }
            
            resp = await self._serviceLogin('serviceLoginAuth2', auth_data)
            
            # Obsługa weryfikacji tożsamości
            if 'notificationUrl' in resp:
                raise XiaomiLoginError("VERIFY", f"Wymagana weryfikacja. Otwórz link: {resp['notificationUrl']}")

            if resp.get('code') != 0:
                if resp.get('code') == 70016:
                    raise XiaomiLoginError(70016, "Błąd 70016: Niepoprawne hasło lub blokada konta. Sprawdź .env lub zaloguj się ręcznie.")
                raise XiaomiLoginError(resp.get('code'), f"Błąd etapu 2: {resp}")

            # Zapisanie tokenów sesji
            self.token['userId'] = resp['userId']
            self.token['passToken'] = resp['passToken']

            # Krok 3: Pobranie tokenów specyficznych dla usługi
            logging.info(f"Pobieranie tokenu dostępu dla {resp['sid']}...")
            serviceToken = await self._securityTokenService(resp['location'], resp['nonce'], resp['ssecurity'])
            self.token[resp['sid']] = (resp['ssecurity'], serviceToken)

            # Jeśli potrzebujemy sid innego niż ten, który zadziałał (np. xiaomi_wear)
            if sid != resp['sid']:
                logging.info(f"Pobieranie tokenu dla usługi docelowej: {sid}")
                resp_sid = await self._serviceLogin(f'serviceLogin?sid={sid}&_json=true')
                if resp_sid.get('code') == 0:
                    st = await self._securityTokenService(resp_sid['location'], resp_sid['nonce'], resp_sid['ssecurity'])
                    self.token[sid] = (resp_sid['ssecurity'], st)

            if self.token_store:
                await self.token_store.save_token(self.token)
            logging.info("ZALOGOWANO POMYŚLNIE.")
            return True

        except XiaomiLoginError:
            raise
        except Exception as e:
            logging.error(f"Wyjątek podczas logowania: {e}")
            return False

    async def mi_request(self, sid, url, data, headers, relogin=True):
        """Obsługa żądań do API Xiaomi Wear/Fitness."""
        if (self.token and sid in self.token) or await self.login(sid):
            session = await self._get_session()
            cookies = {
                'userId': str(self.token['userId']),
                'serviceToken': self.token[sid][1]
            }
            
            # Nagłówki specyficzne dla Mi Fitness
            headers['User-Agent'] = 'MiFitness/3.18.0 (Android; 12)'
            
            method = 'POST' if data else 'GET'
            async with session.request(method, url, json=data, cookies=cookies, headers=headers) as r:
                if r.status == 200:
                    return await r.json(content_type=None)
                elif r.status == 401 and relogin:
                    logging.warning("Token wygasł, odświeżanie...")
                    self.token.pop(sid, None)
                    return await self.mi_request(sid, url, data, headers, False)
                else:
                    text = await r.text()
                    raise Exception(f"Błąd API {r.status}: {text}")
        return None

class MiHealth:
    def __init__(self, account):
        self.account = account
        self.server = "https://api.fitness.xiaomi.com"

    async def get_workout_list(self, limit=10):
        url = f"{self.server}/v1/workout/list?limit={limit}"
        return await self.account.mi_request("xiaomi_wear", url, None, {})

    async def get_workout_detail(self, workout_id):
        url = f"{self.server}/v1/workout/detail?workoutId={workout_id}"
        return await self.account.mi_request("xiaomi_wear", url, None, {})

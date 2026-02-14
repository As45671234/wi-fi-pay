import hashlib

class FreedomPayManager:
    def __init__(self, merchant_id: str, secret_key: str):
        self.merchant_id = merchant_id
        self.secret_key = secret_key
        self.base_url = "https://api.paybox.money/init_payment.php"

    def generate_signature(self, script_name: str, params: dict) -> str:
        """
        Генерирует подпись pg_sig согласно документации Freedom Pay.
        """
        # 1. Исключаем саму подпись из списка параметров
        sorted_params = {k: v for k, v in params.items() if k != 'pg_sig'}
        
        # 2. Сортируем ключи по алфавиту
        sorted_keys = sorted(sorted_params.keys())
        
        # 3. Создаем строку: script_name + значения параметров + secret_key
        values_str = ";".join([str(sorted_params[k]) for k in sorted_keys])
        sig_str = f"{script_name};{values_str};{self.secret_key}"
        
        # 4. Возвращаем MD5 хеш
        return hashlib.md5(sig_str.encode('utf-8')).hexdigest()

    def check_signature(self, script_name: str, params: dict) -> bool:
        """
        Проверяет подпись, пришедшую от банка в Callback.
        """
        received_sig = params.get('pg_sig')
        generated_sig = self.generate_signature(script_name, params)
        return received_sig == generated_sig

# Инициализация (данные из личного кабинета Freedom Pay)
# fp_manager = FreedomPayManager(merchant_id="555xxx", secret_key="ваша_соль")
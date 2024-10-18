from django.conf import settings
from django.contrib.auth.models import BaseUserManager

from sage_auth.strategies.combined_strategy import CombinedStrategy
from sage_auth.strategies.email_strategy import EmailStrategy
from sage_auth.strategies.phone_strategy import PhoneStrategy
from sage_auth.strategies.username_strategy import UsernameStrategy
from sage_auth.utils import set_required_fields


class AuthUserManager(BaseUserManager):
    def get_authentication_strategies(self, user_data):
        """
        Dynamically determine the authentication strategies based on the settings
        and the provided user data.
        """
        strategies = []
        methods = settings.AUTHENTICATION_METHODS

        if methods.get("EMAIL_PASSWORD") and "email" in user_data:
            strategies.append(EmailStrategy())
        if methods.get("PHONE_PASSWORD") and "phone_number" in user_data:
            strategies.append(PhoneStrategy())
        if methods.get("USERNAME_PASSWORD") and "username" in user_data:
            strategies.append(UsernameStrategy())

        if not strategies:
            raise ValueError("No valid authentication method found.")

        if len(strategies) == 1:
            return strategies[0]
        else:
            return CombinedStrategy(strategies)

    def create_user(self, **extra_fields):
        """Dynamically create a user using the selected strategy."""
        username_field, required_fields = set_required_fields()

        # Collect user data
        user_data = {}
        if "email" in required_fields or "email" in extra_fields:
            user_data["email"] = extra_fields.get("email") or input("Email: ")
        if "phone_number" in required_fields or "phone_number" in extra_fields:
            user_data["phone_number"] = extra_fields.get("phone_number") or input(
                "Phone number: "
            )
        if "username" in required_fields or "username" in extra_fields:
            user_data["username"] = extra_fields.get("username") or input("Username: ")
        user_data["password"] = extra_fields.get("password") or input("Password: ")

        user_data["is_staff"] = extra_fields.get("is_staff", False)
        user_data["is_superuser"] = extra_fields.get("is_superuser", False)

        strategy = self.get_authentication_strategies(user_data)
        # strategy.validate(user_data)
        user = strategy.create_user(user_data)
        return user

    def create_superuser(self, **extra_fields):
        """Create a superuser using the same strategy-based logic."""
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        return self.create_user(**extra_fields)

    def authenticate_user(self, user_data):
        """Authenticate a user using the dynamic strategy."""
        strategy = self.get_authentication_strategies(user_data)
        strategy.validate(user_data)
        return strategy.authenticate(user_data)
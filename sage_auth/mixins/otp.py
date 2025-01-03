import logging
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.shortcuts import redirect
from django.utils import timezone as tz
from django.utils.translation import gettext_lazy as _
from django.views import View

from sage_otp.models import OTP
from sage_otp.repository.managers.otp import OTPManager
from sage_otp.helpers.choices import OTPState, ReasonOptions

from sage_auth.models import SageUser
from sage_auth.utils import get_backends, send_email_otp
from sage_auth.signals import otp_expired, otp_failed, otp_verified

logger = logging.getLogger(__name__)


class VerifyOtpMixin(View):
    """
    Mixin for verifying OTPs in user authentication and reactivation flows.

    This mixin provides a secure, reusable structure for OTP verification
    in Django views, supporting use cases like email or phone number activation
    and password recovery. It checks the validity of the OTP, manages failed attempts, and
    initiates account activation if the OTP is verified successfully.
    """

    otp_manager = OTPManager()
    user_identifier = None
    lockout_duration = getattr(settings, "OTP_LOCKOUT_DURATION", 1)
    lock_user = getattr(settings, "OTP_MAX_REQUEST_TIMEOUT", 4)
    block_count = getattr(settings, "OTP_BLOCK_COUNT", 1)
    reason = ReasonOptions.EMAIL_ACTIVATION
    success_url = None
    minutes_left_expiry = None
    seconds_left_expiry = None
    reactivate_process = False

    def setup(self, request, *args, **kwargs):
        self.user_identifier = request.session.get("email")
        logger.debug("Setting up VerifyOtpMixin with user identifier: %s", self.user_identifier)
        return super().setup(request, *args, **kwargs)

    def dispatch(self, request, *args, **kwargs):
        user = self.get_user_by_identifier()
        if user.is_block:
            messages.error(
                self.request, _("Your account has been blocked. Please contact support.")
            )
            logger.warning("Blocked user tried to access: %s", user.email or user.phone_number)
            return redirect(settings.LOGIN_URL)

        if not self.reactivate_process:
            if not request.session.get("spa"):
                logger.warning("Unauthorized access attempt: no active signup session for user identifier: %s", self.user_identifier)
                messages.error(
                    self.request, _("Unauthorized access detected. Please start the signup process again.")
                )
                return redirect(settings.LOGIN_URL)

        logger.info("Dispatching request for user identifier: %s", self.user_identifier)
        return super().dispatch(request, *args, **kwargs)

    def verify_otp(self, user_identifier, entered_otp):
        try:
            logger.debug("Verifying OTP for identifier: %s", user_identifier)
            user = self.get_user_by_identifier()
            otp_instance = self.otp_manager.get_otp(
                identifier=user.id, reason=self.reason
            )
            otp_max = getattr(settings, "OTP_MAX_FAILED_ATTEMPTS", 4)
            otp_expiry_time = otp_instance.last_sent_at + timedelta(
                seconds=self.otp_manager.EXPIRE_TIME.seconds
            )
            time_left_to_expire = (otp_expiry_time - tz.now()).total_seconds()

            if time_left_to_expire <= 0:
                logger.warning("OTP expired for user identifier: %s", user_identifier)
                otp_instance.update_state(OTPState.EXPIRED)
                otp_expired.send(sender=self.__class__, user=user, reason=self.reason)
                messages.error(
                    self.request,
                    _("Your OTP has expired. A new OTP has been sent to your registered contact."),
                )
                self.send_new_otp(user)
                return False

            if otp_instance.failed_attempts_count >= otp_max:
                logger.warning("Too many failed OTP attempts for user identifier: %s", user_identifier)
                otp_failed.send(
                    sender=self.__class__,
                    user=user,
                    reason=self.reason,
                    attempts=otp_instance.failed_attempts_count,
                )
                messages.error(
                    self.request,
                    _("Too many incorrect attempts. A new OTP has been sent to your registered contact."),
                )
                self.send_new_otp(user)
                return False

            if otp_instance.token == entered_otp:
                logger.info("OTP verified successfully for user identifier: %s", user_identifier)
                user.is_active = True
                otp_instance.state = OTPState.CONSUMED
                user.save()
                otp_instance.save()
                otp_verified.send(
                    sender=self.__class__, user=user, success=True, reason=self.reason
                )
                messages.success(
                    self.request, _("OTP verified successfully. You can now proceed.")
                )
                return user
            else:
                logger.warning("Incorrect OTP entered for user identifier: %s", user_identifier)
                otp_instance.failed_attempts_count += 1
                otp_instance.save()
                otp_failed.send(
                    sender=self.__class__,
                    user=user,
                    reason=self.reason,
                    attempts=otp_instance.failed_attempts_count,
                )
                messages.error(
                    self.request,
                    _("Incorrect OTP. Please try again."),
                )
                return False

        except SageUser.DoesNotExist:
            logger.error("Failed to retrieve user by identifier: %s", user_identifier)
            messages.error(
                self.request,
                _("Invalid user identifier. Please try again or restart the process."),
            )
            return False

        except Exception as e:
            logger.exception("Unexpected error during OTP verification for user identifier: %s", user_identifier)
            messages.error(
                self.request,
                _("An unexpected error occurred during OTP verification. Please try again later."),
            )
            otp_failed.send(sender=self.__class__, user=None, reason=self.reason, attempts=0)
            return False

    def locked_user(self, count):
        logger.debug("Checking if user identifier %s is locked. Attempt count: %d", self.user_identifier, count)
        return count >= self.lock_user

    def handle_locked_user(self):
        lockout_start_time = self.request.session.get("lockout_start_time")
        if lockout_start_time:
            lockout_start_time = tz.datetime.fromisoformat(lockout_start_time)
            now = tz.now()
            time_passed = (now - lockout_start_time).total_seconds()
            time_left = (self.lockout_duration * 60) - time_passed
            minutes_left = int(time_left // 60)
            seconds_left = int(time_left % 60)
            if time_left > 0:
                logger.info("User identifier %s is in lockout period. Time left: %d seconds", self.user_identifier, time_left)
                messages.error(
                    self.request,
                    _(f"Too many attempts. Please wait {minutes_left} minutes and {seconds_left} seconds before trying again."),
                )
            else:
                logger.info("Lockout period ended for user identifier: %s", self.user_identifier)
                self.request.session["max_counter"] = 0
                del self.request.session["lockout_start_time"]
                messages.info(
                    self.request,
                    _("You can now try again."),
                )

        return self.render_to_response(self.get_context_data())

    def post(self, request, *args, **kwargs):
        count = request.session.get("max_counter", 0)
        block = self.request.session.get("block_count", 0)
        logger.debug("POST request received for OTP verification. Count: %d, Block: %d", count, block)
        if self.block_count <= block:
            logger.warning("User identifier %s has been blocked due to too many failed attempts", self.user_identifier)
            self.block_user()
            messages.error(
                self.request,
                _("You have been blocked due to multiple failed OTP attempts."),
            )
            return redirect(request.path)

        if not self.locked_user(count):
            request.session["max_counter"] = count + 1
            entered_otp = request.POST.get("verify_code")
            logger.debug("Verifying OTP for user identifier: %s", self.user_identifier)
            user = self.verify_otp(self.user_identifier, entered_otp)
            if user:
                logger.info("OTP verification successful for user identifier: %s", self.user_identifier)
                if self.reason == ReasonOptions.FORGET_PASSWORD:
                    pass
                else:
                    login(request, user)
                self.clear_session_keys(["spa", "block_count", "max_counter"])
                return redirect(self.get_success_url())
            logger.info("OTP verification failed for user identifier: %s", self.user_identifier)
            return self.render_to_response(self.get_context_data())
        else:
            if not self.request.session.get("lockout_start_time"):
                request.session["lockout_start_time"] = tz.now().isoformat()
                request.session["block_count"] = block + 1
            logger.info("Handling locked user scenario for user identifier: %s", self.user_identifier)
            return self.handle_locked_user()

    def get_success_url(self):
        if not self.success_url:
            logger.error("Success URL is not set for user identifier: %s", self.user_identifier)
            raise ValueError("The success_url attribute is not set.")
        logger.debug("Redirecting to success URL: %s for user identifier: %s", self.success_url, self.user_identifier)
        return self.success_url

    def send_new_otp(self, user):
        logger.info("Sending new OTP to user identifier: %s", self.user_identifier)
        if "@" in self.user_identifier:
            otp_data = self.otp_manager.get_or_create_otp(
                identifier=user.id, reason=self.reason
            )
            send_email_otp(otp_data[0].token, user.email)
            logger.debug("New OTP sent via email to: %s", user.email)
            messages.info(self.request, _("A new OTP has been sent to your email address."))
        else:
            otp_data = self.otp_manager.get_or_create_otp(
                identifier=user.id, reason=self.reason
            )
            sms_obj = get_backends()
            sms_obj.send_one_message(str(user.phone_number), otp_data[0].token)
            logger.debug("New OTP sent via SMS to: %s", user.phone_number)
            messages.info(
                self.request,
                _("A new OTP has been sent to your phone number."),
            )

    def get_context_data(self, **kwargs):
        context = kwargs or {}
        context["minutes_left_expiry"] = self.minutes_left_expiry
        context["seconds_left_expiry"] = self.seconds_left_expiry
        logger.debug("Context data prepared for user identifier: %s", self.user_identifier)
        return context

    def block_user(self):
        user = self.get_user_by_identifier()
        user.is_block = True
        user.is_active = False
        user.save()
        logger.warning("User identifier %s has been blocked", self.user_identifier)
        reason = self.request.session.get("reason")
        try:
            otp_instance = self.otp_manager.get_otp(identifier=user.id, reason=reason)
        except OTP.DoesNotExist:
            logger.error("Failed to retrieve OTP token for blocking user identifier: %s", self.user_identifier)
            messages.error(
                self.request,
                _("OTP Token Does not Exist.")
            )
            return
        otp_instance.state = OTPState.EXPIRED
        otp_instance.save()
        logger.debug("All OTP instances for user identifier %s have been expired", self.user_identifier)
        messages.error(
            self.request,
            _("Your account has been blocked due to multiple failed OTP attempts. Please contact support."),
        )

    def get_user_by_identifier(self):
        if "@" in self.user_identifier:
            logger.debug("Retrieving user by email: %s", self.user_identifier)
            return SageUser.objects.get(email=self.user_identifier)
        logger.debug("Retrieving user by phone number: %s", self.user_identifier)
        return SageUser.objects.get(phone_number=self.user_identifier)

    def clear_session_keys(self, keys):
        for key in keys:
            if key in self.request.session:
                logger.debug("Clearing session key: %s", key)
                del self.request.session[key]

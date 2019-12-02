from django.db import models, IntegrityError
from django.contrib.auth.models import User
from django.utils.translation import ugettext_lazy as _
from django.utils.translation import ugettext as _not_lazy
from django.utils.functional import cached_property
from django.utils import timezone
from django.conf import settings
from django.core.validators import RegexValidator, MinValueValidator, MaxValueValidator
from django.core.exceptions import ValidationError
from django.core.urlresolvers import reverse
from django.dispatch import Signal
from django.utils import formats

import uuid
import random

from decimal import *
from datetime import datetime, timedelta, date
import arrow
from dateutil.relativedelta import relativedelta
import calendar
import operator

from pandas.tseries.offsets import BDay  # BDay is business day, not birthday. used for expected_execution_on in Payout

from localflavor.generic.models import IBANField, BICField
from localflavor.generic.countries.sepa import IBAN_SEPA_COUNTRIES
from djmoney.models.fields import MoneyField as DjMoneyField, Money

from .settings import (
    PaymentProvider,
    DISCOUNT_CODE_TYPES,
    CODE_LENGTH,
    CODE_CHARS,
    SEGMENTED_CODES,
    SEGMENT_LENGTH,
    SEGMENT_SEPARATOR,
)
from .constants import *
from .managers import ActiveStatusManager, ActivePaymentAuthorizationParentStatusManager
from . import signals  #! important for signals to work

from courses.models import Enrollment, Reservation
from courses.tasks import run_task, send_transactional_mail, COURSE_PURCHASE_CONFIRM, COURSE_PURCHASE_NOTIFY,\
    SINGLE_LESSON_PURCHASE_CONFIRM, SINGLE_LESSON_PURCHASE_NOTIFY, CLASS_CARD_PURCHASE_NOTIFY,\
    CLASS_CARD_PURCHASE_CONFIRM
from payments.tasks import propagate_save_to_products
from invoices.models import Invoice, InvoiceItem

from canvas import utils

from invoices import tasks as invoice_tasks
from verifications import models as verification_models




class Product(models.Model):
    '''
    Parent class for CourseProduct, BookingProduct, MerchProduct
    '''
    professional = models.ForeignKey(User, on_delete=models.CASCADE)
    discounts = models.ManyToManyField('Discount', blank=True)
    vat = models.DecimalField(
        _("Sales Tax"),
        max_digits=2,
        decimal_places=2,
        blank=False,
        null=True,
        default=DEFAULT_NL,
        choices=VAT_CHOICES)
    _has_early_bird = models.BooleanField(default=False)

    class Meta:
        verbose_name = _("Product")

    def __str__(self):
        return f'Product by {self.professional.get_full_name()}'

    def save(self, *args, **kwargs):
        if self.discounts.filter(_is_early_bird=True).exists():
            self._has_early_bird = True
        super().save(*args, **kwargs)

    @property
    def vat_as_integer(self):
        return int(self.vat * 100)

    @property
    def has_early_bird(self) -> bool:
        # convenience property
        return self._has_early_bird


class CourseProduct(Product):
    packages = models.ManyToManyField(
        'payments.Package',
        through='payments.CourseProductPackage',
        related_name='course_product',
        help_text=_("Which purchase options do you offer the participants for the course?")
    )
    intake = models.CharField(
        _("Course intake"),
        max_length=30,
        choices=INTAKE_CHOICES,
        default=FULL_INTAKE,
        help_text=_("Can participants join the course after it has started?")
    )
    stackable_discounts = models.BooleanField(
        _("Stack discounts"),
        default=False,
        help_text=_("Can participants use more than one discount per purchase?")
    )

    def __str__(self):
        return _(f"Product by {self.professional.get_full_name()}")

    @property
    def has_single_lessons(self):
        ''' used in template, convenience property for self.has_package_kind() '''
        return self.has_package_kind(SINGLE)

    @property
    def has_class_cards(self):
        ''' used in template, convenience property for self._has_package_kind() '''
        return self.has_package_kind(CLASS_CARD)

    @property
    def package_with_lowest_standard_price(self) -> 'Package':
        return self.packages.filter(course_product_packages__is_active=True).order_by('price').first()

    @property
    def package_with_highest_standard_price(self) -> 'Package':
        return self.packages.filter(course_product_packages__is_active=True).order_by('-price').first()

    @cached_property
    def all_public_prices(self) -> dict:
        ''' returns prices for all public discounts '''
        prices = []
        course_product_packages = self.course_product_packages.filter(is_active=True)
        for course_product_package in course_product_packages:
            # standard rate
            prices.append({
                'course_product_package': course_product_package,
                'discount_condition': None,
                'discounted_by': None,
                'price': course_product_package.package.price,
            })
            # discounted rates
            for discount in self.discounts.filter(public=True):
                # don't show passed early bird discounts or future last-minute discounts
                # (ineligible independently from user)
                if not discount.is_over:
                    (price, discounted_by) = discount.apply(
                        price=course_product_package.package.price,
                        course_product_package=course_product_package
                    )
                    if discounted_by:
                        prices.append({
                            'course_product_package': course_product_package,
                            'discount_condition': discount,
                            'discounted_by': discounted_by,
                            'price': price,
                        })
        return prices

    # methods
    def has_package(self, package:'Package') -> bool:
        return package in self.packages.filter(course_product_packages__is_active=True)

    def has_package_kind(self, package_kind:str) -> bool:
        ''' product has complete/single/subscription package? '''
        return self.packages.filter(kind=package_kind).exists()

    def get_packages_with_personalized_prices(self, user):
        ''' gets prices for all offered packages, adjusted to the user '''
        package_prices = []
        for package in self.course_product_packages.filter(is_active=True):  # looping through all possible packages
            package_prices.append(package.personalized_prices(user=user))
        package_prices.sort(key=lambda x: x['priority'])
        return package_prices


class Package(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='packages', null=True, blank=True)
    team = models.ForeignKey('teams.Team', on_delete=models.CASCADE, related_name='packages', null=True, blank=True)
    kind = models.CharField(max_length=15, choices=PRICE_PACKAGE_CHOICES, default=COMPLETE)
    price = MoneyField(
        _("Price for this package"),
        max_digits=10,
        decimal_places=2,
        default_currency='EUR',
        blank=False,
        help_text=_(
            "This is the price a participant pays for one purchase of this package."),
    )
    # for class card
    lesson_amount = models.IntegerField(
        _("Amount of single lessons per class card"),
        blank=True,
        null=True,
        help_text=_("How many single lessons can a student take with this class card?"),
    )
    # for subscription
    renewal_frequency = models.CharField(
        _("Subscription renewal frequency"),
        max_length=50,
        choices=RENEWAL_CHOICES,
        blank=True,
        null=True,
        help_text=_("How often does a student get charged with this subscription package?"),
    )
    # for trials
    is_trial = models.BooleanField(default=False)
    trial_expiration = models.DurationField(
        _("Time after which a student can join another trial"), blank=True, null=True,
    )

    def __str__(self):
        return _not_lazy(f"{self.get_kind_display()} for {self.price}")

    def clean(self):
        # Either user or team needs to be set
        if (self.user and self.team) or (not self.user and not self.team):
            raise ValidationError(_("You have to set either a user or a team."))
        # Don't allow draft entries to have a pub_date.
        if self.kind == SUBSCRIPTION and self.renewal_frequency is None:
            raise ValidationError(_("Subscriptions need a renewal frequency."))
        elif self.kind == CLASS_CARD and self.lesson_amount is None:
            raise ValidationError(_("Class cards need a lesson amount."))

    @property
    def owner(self):
        return self.user if self.user else self.team

    @property
    def is_team_package(self) -> bool:
        return True if self.team else False

    @property
    def is_teacher_package(self) -> bool:
        return True if self.teacher else False

    @property
    def article(self):
        if self.kind == COMPLETE:
            return _("the")
        else:
            return _("a")

    @property
    def payment_recurrence_string(self) -> str:
        ''' returns payment recurrence string for this package, eg "once" or "weekly" '''
        if self.kind == COMPLETE or self.kind == SINGLE or self.kind == CLASS_CARD:
            return _("once")
        return self.get_renewal_frequency_display()

    @property
    def facebook_pixel_event(self):
        ''' returns css-class for specific facebook pixel event '''
        options = {
            COMPLETE: 'addCompleteToCartEvent',
            SUBSCRIPTION: 'addSubscriptionToCartEvent',
            CLASS_CARD: 'addCardToCartEvent',
            SINGLE: 'addSingleToCartEvent',
        }
        return options[self.kind]

    @property
    def priority(self) -> int:
        ''' returns priority which can be used for suggestions or layouts, eg highlight a package with highest priority '''
        if self.kind == COMPLETE or self.kind == SUBSCRIPTION:
            return 10
        if self.kind == CLASS_CARD:
            return 30
        if self.kind == SINGLE:
            return 50


class CourseProductPackage(models.Model):
    course_product = models.ForeignKey(
        'payments.CourseProduct',
        on_delete=models.CASCADE,
        related_name='course_product_packages'
    )
    package = models.ForeignKey('payments.Package', on_delete=models.CASCADE, related_name='course_product_packages')
    is_active = models.BooleanField(_("Is this package bookable"), default=True)

    def __str__(self):
        return _not_lazy(f'{self.package.get_kind_display()}')

    def get_intake_price_adjustment(self, join_datetime=timezone.now()) -> Decimal:
        ''' returns intake price adjustment as percentage if applicable '''
        if self.course_product.intake == ADJUSTED_INTAKE\
                and self.package.kind != SINGLE\
                and self.package.kind != CLASS_CARD:
            # find payment period:
            if self.package.kind == COMPLETE:
                # find the start and end of the course
                start = self.course_product.course.start
                if self.course_product.course.end_recurring_period:
                    end = self.course_product.course.end_recurring_period
                else:
                    end = self.course_product.course.start + relativedelta(years=2)
            else:
                # find the start of billing cycle // end of billing cycle
                if self.package.renewal_frequency in [WEEKLY, THIRTY_DAYS, THREEHUNDERTSIXTYFIVE_DAYS, TRIGGERED]:
                    # subscriptions with own billing cycle don't have intakes
                    return 1.00
                elif self.package.renewal_frequency == MONTHLY:
                    start = timezone.now().replace(day=1)
                    last_day = calendar.monthrange(timezone.now().year, timezone.now().month)[1]
                else:  # YEARLY
                    start = timezone.now().replace(day=1).replace(month=1)
                    last_day = calendar.monthrange(timezone.now().year, 12)
                end = timezone.now().replace(day=last_day).replace(month=12)
            # find total amount of lessons in this payment period
            total_lessons = len(self.course_product.course.get_occurrences(start, end))
            if total_lessons == 0:
                return 1.00
            # find remaining amount of lessons in this payment period
            remaining_lessons = len(self.course_product.course.get_occurrences(join_datetime, end))
            # convert to percentage
            return round(Decimal(remaining_lessons / total_lessons), 10)
        else:
            return 1.00

    def apply_intake_price_adjustment(self, price:Money, join_datetime=timezone.now()) -> Money:
        ''' applies intake price adjustment to price, returned amount should always be a Decimal (2) '''
        amount = round(price.amount * self.get_intake_price_adjustment(join_datetime),2)
        return Money(amount, price.currency)

    def get_eligible_discounts_or_redirect_url(self, user) -> dict:
        ''' loops through product's discounts and finds the ones the user is eligible for '''
        results = {
            'redirect': None,
            'eligible_for': [],
        }
        for discount in self.course_product.discounts.all():
            result = discount.is_eligible_for(user=user, course=self.course_product.course, course_product_package=self)
            if result['redirect']:
                # we need more info
                results['redirect'] = result['redirect']
                return results
            elif result['is_eligible'] == True:
                results['eligible_for'].append(discount)
        return results

    def get_final_price_or_redirect_url(self, user, join_datetime=timezone.now()) -> dict:
        ''' returns a dict with final, eligible price for this user and package - or a redirect url to get more info '''
        discounts = self.get_eligible_discounts_or_redirect_url(user=user)
        results = {
            'redirect': discounts['redirect'],
            'eligible_for': discounts['eligible_for'],
            'original_price': self.package.price,
            'final_price': self.package.price,
            'joining_late_on': None,
            'leaving_early_on': None
        }

        # if course doesn't allow stackable discounts: (default false)
        # find highest discounts in discounts['eligible_for'] and delete the other discounts from the variable
        if not self.course_product.stackable_discounts:
            highest_discount = self._get_highest_discount(discounts=results['eligible_for']) # can be None
            results['eligible_for'] = [highest_discount] if highest_discount else []

        if results['redirect']:
            return results
        else:
            for discount in results['eligible_for']:
                # apply discounts
                results['final_price'], str = discount.apply(price=results['final_price'], package=self.package)

        # get and apply intake price adjustment
        intake_price_adjustment = self.get_intake_price_adjustment(join_datetime=join_datetime)
        if intake_price_adjustment < 1.00:
            results['final_price'] = self.apply_intake_price_adjustment(price=results['final_price'], join_datetime=join_datetime)
            results['eligible_for'].append(_("late intake"))
            results['joining_late_on'] = join_datetime
        return results

    def personalized_prices(self, user) -> dict:
        ''' encloses all information of eligible discounts, redirect urls and final prices in one dict '''
        prices = self.get_final_price_or_redirect_url(user=user)
        return ({
            'name': str(self),
            'kind': self.package.kind,
            'priority': self.package.priority,
            'final_price': str(prices['final_price']),
            'original_price': str(prices['original_price']) if self._get_is_discounted(prices) else None,
            'discounted_by': "{}%".format(int(round(1 - (prices['final_price'] / prices['original_price']),
                                                2) * 100)) if self._get_is_discounted(prices) else None,
            'discounts': prices['eligible_for'] if self._get_is_discounted(prices) else None
        })

    def _get_is_discounted(self, prices) -> bool:
        return (prices['final_price'] != prices['original_price'])

    def _get_highest_discount(self, discounts):
        ''' returns this package's  highest discount and the discounted price '''
        (highest_discount, cheapest_price) = (None, self.package.price)
        for discount in discounts:
            discounts_final_price, unused_string = discount.apply(price=self.package.price, package=self.package)
            if discounts_final_price < cheapest_price:
                (highest_discount, cheapest_price) = (discount, discounts_final_price)
        return highest_discount
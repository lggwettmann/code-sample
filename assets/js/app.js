// Discount Setup Formset
var rowHandling = function ($formRow) {
  var conditionalDiscountFields = $formRow.find(
    'div[id*="div_id_form-"][id*="-age_operator"], ' +
    'div[id*="div_id_form-"][id*="-age_value"], ' +
    'div[id*="div_id_form-"][id*="-early_bird_duration"], ' +
    'div[id*="div_id_form-"][id*="-document_choice"]'
  )
  var conditionalAmountFields = $formRow.find(
    'div[id*="div_id_form-"][id*="-discount_percentage"], ' +
    'div[id*="div_id_form-"][id*="-discount_money"]'
  )
  var currencyFields = $formRow.find('select[id*="id_form-"][id*="-discount_money_1"]')
  var priceFields = $formRow.find('input[id*="id_form-"][id*="-discount_money_0"]')
  var durationFields = $formRow.find('input[id*="id_form-"][id*="-early_bird_duration_"]')
  conditionalDiscountFields.hide()
  conditionalAmountFields.hide()
  currencyFields.hide()
  // Add bootstrap styling to price and duration fields
  priceFields.addClass('form-control').wrap('<div class="input-group-prepend"></div>')
  durationFields.addClass('form-control')
  var DiscountConditions = [
    {
      label: $('input[id*="id_form-"][id*="-discount_kind_1"]'),
      conditionalFields: $('div[id*="div_id_form-"][id*="-age_operator"], div[id*="div_id_form-"][id*="-age_value"]')
    },
    {
      label: $('input[id*="id_form-"][id*="-discount_kind_2"]'),
      conditionalFields: $('div[id*="div_id_form-"][id*="-early_bird_duration"]')
    },
    {
      label: $('input[id*="id_form-"][id*="-discount_kind_3"]'),
      conditionalFields: $('div[id*="div_id_form-"][id*="-document_choice"]')
    }
  ]
  var AmountConditions = [
    {
      label: $('input[id*="id_form-"][id*="-discount_calculation_1"]'),
      conditionalFields: $('div[id*="div_id_form-"][id*="-discount_percentage"]')
    },
    {
      label: $('input[id*="id_form-"][id*="-discount_calculation_2"]'),
      conditionalFields: $('div[id*="div_id_form-"][id*="-discount_money"]')
    }
  ]
  // Show the conditional fields if click on label
  DiscountConditions.forEach((item) => {
    var $internalLabel = $formRow.find(item.label)
    if ($internalLabel.prop('checked')) {
      $formRow.find(item.conditionalFields).show()
    }
    $internalLabel.change({ $formRow: $formRow, item: item }, function () {
      conditionalDiscountFields.slideUp()
      $formRow.find(item.conditionalFields).slideToggle()
    })
  }, $formRow)
  AmountConditions.forEach((item) => {
    var $internalLabel = $formRow.find(item.label)
    if ($internalLabel.prop('checked')) {
      $formRow.find(item.conditionalFields).show()
    }
    $internalLabel.change({ $formRow: $formRow, item: item }, function () {
      conditionalAmountFields.slideUp()
      $formRow.find(item.conditionalFields).slideToggle()
    })
  }, $formRow)
}

$(function () {
  $('.dynamic-formset-row').each(function () {
    rowHandling($(this))
  })
  $('.dynamic-formset-row').formset({
    addText: '',
    deleteText: '',
    addCssClass: 'btn btn-lgX btn-block btn-success fa fa-plus pull-left mb-3',
    deleteCssClass: 'btn btn-block btn-danger fa fa-trash pull-right mb-1',
    hideLastAddForm: true,
    added: rowHandling
  })
})

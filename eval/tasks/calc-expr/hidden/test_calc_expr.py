import copy
import unittest

import calc_expr


class CalcTestCase(unittest.TestCase):
    def check(self, source, expected, variables=None):
        result = calc_expr.evaluate(source, variables)
        self.assertEqual(result, expected)
        self.assertIs(type(result), type(expected))

    def check_error(self, source, error, column, variables=None):
        with self.assertRaises(error) as caught:
            calc_expr.evaluate(source, variables)
        self.assertEqual(caught.exception.column, column)
        return caught.exception

    def check_unexpected(self, source, column, variables=None):
        return self.check_error(source, calc_expr.UnexpectedTokenError, column, variables)


class WhitespaceTest(CalcTestCase):  # rule 1
    def test_all_four_whitespace_characters(self):
        self.check("\t1 +\n2\r\n", 3)
        self.check("1+2*3", 7)

    def test_non_breaking_space_invalid(self):
        self.check_unexpected("1 +\u00a02", 3)

    def test_vertical_tab_invalid(self):
        self.check_unexpected("1 +\x0b2", 3)

    def test_form_feed_invalid(self):
        self.check_unexpected("1\x0c+ 2", 1)


class InvalidCharacterTest(CalcTestCase):  # rule 2
    def test_non_ascii_letter_invalid(self):
        self.check_unexpected("\u00e9 + 1", 0, {"\u00e9": 1})

    def test_non_ascii_letter_inside_name(self):
        self.check_unexpected("x\u00e9", 1, {"x": 1, "x\u00e9": 2})

    def test_superscript_digit_invalid(self):
        self.check_unexpected("1\u00b2", 1)

    def test_fullwidth_digit_invalid(self):
        self.check_unexpected("1 + \uff12", 4)


class OperatorTokenTest(CalcTestCase):  # rule 3
    def test_spaced_operators_are_two_tokens(self):
        self.check_unexpected("2 * * 3", 4)
        self.check_unexpected("8 / / 2", 4)

    def test_triple_star(self):
        self.check_unexpected("2***3", 3)


class LiteralTest(CalcTestCase):  # rules 4, 5
    def test_integer_and_decimal_literals(self):
        self.check("42", 42)
        self.check("007 + 00", 7)
        self.check("1.5", 1.5)

    def test_trailing_dot(self):
        self.check("1.", 1.0)

    def test_leading_dot(self):
        self.check(".25", 0.25)

    def test_exponent_is_float(self):
        self.check("1e3", 1000.0)
        self.check("1.5E-2", 0.015)
        self.check(".5e+1", 5.0)

    def test_trailing_dot_with_exponent(self):
        self.check("1.e2", 100.0)

    def test_exponent_without_digits_ends_literal(self):
        self.check_unexpected("2e", 1, {"e": 1})
        self.check_unexpected("2E+x", 1, {"E": 1, "x": 1})

    def test_second_dot_starts_new_literal(self):
        self.check_unexpected("1.2.3", 3)

    def test_lone_dot_invalid(self):
        self.check_unexpected("1 + .", 4)

    def test_underscore_not_in_literal(self):
        self.check_unexpected("1_000", 1)


class PrecedenceTest(CalcTestCase):  # rule 7
    def test_binary_precedence_and_left_associativity(self):
        self.check("1 + 2 * 3", 7)
        self.check("10 - 4 - 3", 3)
        self.check("100 / 10 / 5", 2.0)
        self.check("100 % 7 * 2", 4)

    def test_power_right_associative(self):
        self.check("2 ** 3 ** 2", 512)

    def test_unary_minus_below_power(self):
        self.check("-2 ** 2", -4)
        self.check("-x ** 2", -9, {"x": 3})

    def test_power_exponent_takes_unary(self):
        self.check("2 ** -1", 0.5)
        self.check("2**--2", 4)

    def test_power_exponent_unary_then_power(self):
        self.check("2 ** -2 ** 2", 0.0625)

    def test_unary_above_floor_division_and_modulo(self):
        self.check("-7 // 2", -4)
        self.check("-7 % 3", 2)

    def test_repeated_unary(self):
        self.check("--2", 2)
        self.check("-+-2", 2)
        self.check("2---2", 0)

    def test_parentheses(self):
        self.check("(1 + 2) * 3", 9)
        self.check("(-2) ** 2", 4)
        self.check("abs(-2) ** 3", 8)


class ImplicitMultiplicationTest(CalcTestCase):  # rules 7, 21
    def test_number_then_operand(self):
        self.check_unexpected("2 x", 2, {"x": 3})
        self.check_unexpected("2(3)", 1)

    def test_paren_then_paren(self):
        self.check_unexpected("(1)(2)", 3)


class ArithmeticTest(CalcTestCase):  # rules 9, 10, 11
    def test_int_arithmetic_stays_int(self):
        self.check("7 * 6 - 2", 40)
        self.check("3 ** 4", 81)
        self.check("0 ** 0", 1)

    def test_floor_division_and_modulo_floor(self):
        self.check("7 // -2", -4)
        self.check("7 % -2", -1)
        self.check("7.5 // 2", 3.0)
        self.check("7.5 % 2", 1.5)

    def test_true_division_always_float(self):
        self.check("4 / 2", 2.0)

    def test_float_results(self):
        self.check("2 ** -2", 0.25)
        self.check("4 ** 0.5", 2.0)
        self.check("1 + 2.0", 3.0)


class DivisionByZeroTest(CalcTestCase):  # rule 12
    def test_division_and_floor_division(self):
        self.check_error("1 / 0", calc_expr.DivisionByZeroError, 2)
        self.check_error("5 // 0", calc_expr.DivisionByZeroError, 2)

    def test_modulo(self):
        self.check_error("5 % 0", calc_expr.DivisionByZeroError, 2)

    def test_float_and_negative_float_zero(self):
        self.check_error("1 / 0.0", calc_expr.DivisionByZeroError, 2)
        self.check_error("1 // -0.0", calc_expr.DivisionByZeroError, 2)

    def test_zero_to_negative_int_power(self):
        self.check_error("0 ** -1", calc_expr.DivisionByZeroError, 2)

    def test_float_zero_to_negative_float_power(self):
        self.check_error("0.0 ** -0.5", calc_expr.DivisionByZeroError, 4)

    def test_column_of_inner_operator(self):
        self.check_error("1 / (2 / 0)", calc_expr.DivisionByZeroError, 7)
        self.check_error("(1+2) % (3-3)", calc_expr.DivisionByZeroError, 6)

    def test_is_zero_division_error(self):
        error = self.check_error("1/0", calc_expr.DivisionByZeroError, 1)
        self.assertIsInstance(error, ZeroDivisionError)
        self.assertIsInstance(error, calc_expr.CalcError)


class DomainTest(CalcTestCase):  # rule 13
    def test_negative_base_fractional_exponent(self):
        self.check_error("(-8) ** 0.5", calc_expr.DomainError, 5)
        self.check_error("x ** 0.5", calc_expr.DomainError, 2, {"x": -4})

    def test_unary_minus_outside_power_is_fine(self):
        self.check("-8 ** 0.5", -(8 ** 0.5))

    def test_whole_float_exponent_allowed(self):
        self.check("(-8) ** 2.0", 64.0)
        self.check("(-2) ** -1", -0.5)


class OverflowTest(CalcTestCase):  # rule 14
    def test_float_power_overflow(self):
        self.check_error("10.0 ** 400", calc_expr.DomainError, 5)

    def test_int_base_float_exponent_overflow(self):
        self.check_error("1 + 2 ** 2000.0", calc_expr.DomainError, 6)

    def test_int_power_never_overflows(self):
        self.check("2 ** 2000", 2 ** 2000)


class VariableTest(CalcTestCase):  # rules 6, 15
    def test_lookup(self):
        self.check("x * y + z", 6.5, {"x": 2, "y": 3, "z": 0.5})
        self.check("_a1 + b_2", 5, {"_a1": 2, "b_2": 3})

    def test_unknown_name_is_case_sensitive(self):
        error = self.check_error("X", calc_expr.UnknownNameError, 0, {"x": 1})
        self.assertEqual(error.name, "X")
        error = self.check_error("1 + foo", calc_expr.UnknownNameError, 4, {"fo": 1})
        self.assertEqual(error.name, "foo")

    def test_no_variables(self):
        self.check_error("a", calc_expr.UnknownNameError, 0)

    def test_function_name_as_variable(self):
        self.check("max + 1", 3, {"max": 2})

    def test_function_name_without_call_is_unknown(self):
        error = self.check_error("abs", calc_expr.UnknownNameError, 0)
        self.assertEqual(error.name, "abs")

    def test_variables_not_modified(self):
        variables = {"x": 2, "y": 3.5}
        before = copy.deepcopy(variables)
        calc_expr.evaluate("x * 3", variables)
        self.assertEqual(variables, before)


class FunctionTest(CalcTestCase):  # rules 8, 16, 17, 18
    def test_abs_min_max(self):
        self.check("abs(-3)", 3)
        self.check("abs(-2.5)", 2.5)
        self.check("max(3, 9, 4)", 9)
        self.check("min(3, -9, 4)", -9)
        self.check("max(abs(-5), min(2, 3) * 3, 2 ** 2)", 6)

    def test_min_max_single_argument(self):
        self.check("max(5)", 5)
        self.check("min(2.5)", 2.5)

    def test_max_tie_returns_first(self):
        self.check("max(1, 1.0)", 1)
        self.check("max(1.0, 1)", 1.0)

    def test_min_tie_returns_first(self):
        self.check("min(2.0, 2, 3)", 2.0)

    def test_round_halves_to_even(self):
        self.check("round(2.5)", 2)
        self.check("round(3.5)", 4)
        self.check("round(-2.5)", -2)

    def test_round_returns_int(self):
        self.check("round(2.7)", 3)
        self.check("round(7)", 7)

    def test_whitespace_and_newline_before_paren(self):
        self.check("max (1, 2)", 2)
        self.check("abs\n(-1)", 1)

    def test_call_ignores_variable_of_same_name(self):
        self.check("abs(-2)", 2, {"abs": 10})

    def test_calling_a_variable_is_unknown(self):
        error = self.check_error("f(3)", calc_expr.UnknownNameError, 0, {"f": 2})
        self.assertEqual(error.name, "f")

    def test_unknown_function(self):
        error = self.check_error("1 + sqrt(4)", calc_expr.UnknownNameError, 4)
        self.assertEqual(error.name, "sqrt")


class ArityTest(CalcTestCase):  # rule 16
    def check_arity(self, source, column, name, given):
        error = self.check_error(source, calc_expr.ArityError, column)
        self.assertEqual((error.name, error.given), (name, given))

    def test_max_no_arguments(self):
        self.check_arity("max()", 0, "max", 0)

    def test_fixed_arity_too_many(self):
        self.check_arity("abs(1, 2)", 0, "abs", 2)
        self.check_arity("round(2.5, 1)", 0, "round", 2)

    def test_column_of_name(self):
        self.check_arity("1 + min()", 4, "min", 0)


class CallSyntaxTest(CalcTestCase):  # rules 8, 21
    def test_misplaced_commas(self):
        self.check_unexpected("max(1,)", 6)
        self.check_unexpected("max(,1)", 4)
        self.check_unexpected("1, 2", 1)
        self.check_unexpected("(1, 2)", 2)


class EvaluationOrderTest(CalcTestCase):  # rule 19
    def test_syntax_error_before_division_by_zero(self):
        self.check_unexpected("1/0 +", 5)

    def test_syntax_error_before_unknown_name(self):
        self.check_unexpected("unknown 1", 8)

    def test_left_operand_first(self):
        self.check_error("x + 1/0", calc_expr.UnknownNameError, 0)
        self.check_error("1/0 + x", calc_expr.DivisionByZeroError, 1)

    def test_no_short_circuit(self):
        self.check_error("0 * (1/0)", calc_expr.DivisionByZeroError, 6)

    def test_arity_before_arguments(self):
        self.check_error("abs(1/0, 2)", calc_expr.ArityError, 0)

    def test_unknown_function_before_arguments(self):
        self.check_error("foo(1/0)", calc_expr.UnknownNameError, 0)

    def test_arguments_left_to_right(self):
        self.check_error("max(x, 1/0)", calc_expr.UnknownNameError, 4)


class ColumnTest(CalcTestCase):  # rule 20
    def test_columns_count_every_character(self):
        self.check_error("1 +\n  y", calc_expr.UnknownNameError, 6)
        self.check_unexpected("1 +\n\t* 2", 5)


class LeftmostTokenTest(CalcTestCase):  # rule 21
    def test_bad_token_before_invalid_character(self):
        self.check_unexpected("1 2 $", 2)
        self.check_unexpected("x y $", 2, {"x": 1, "y": 2})

    def test_bad_token_in_parens_before_invalid_character(self):
        self.check_unexpected("(1 2 $)", 3)

    def test_invalid_character_where_operand_expected(self):
        self.check_unexpected("1 + $", 4)

    def test_unmatched_and_empty_parens(self):
        self.check_unexpected("1)", 1)
        self.check_unexpected("(1))", 3)
        self.check_unexpected("()", 1)

    def test_binary_operator_without_left_operand(self):
        self.check_unexpected("* 2", 0)
        self.check_unexpected("1 + * 2", 4)


class EndOfInputTest(CalcTestCase):  # rule 22
    def test_dangling_operator(self):
        self.check_unexpected("1 +", 3)

    def test_trailing_whitespace_counts(self):
        self.check_unexpected("1 +  ", 5)

    def test_lone_unary_operator(self):
        self.check_unexpected("- ", 2)

    def test_empty_and_blank_source(self):
        self.check_unexpected("", 0)
        self.check_unexpected("  \t", 3)

    def test_lone_paren(self):
        self.check_error("(", calc_expr.UnclosedParenError, 0)

    def test_operand_missing_inside_paren(self):
        self.check_error("(1 +", calc_expr.UnclosedParenError, 0)

    def test_innermost_open_paren(self):
        self.check_error("(1 + (2", calc_expr.UnclosedParenError, 5)

    def test_closed_inner_paren_reports_outer(self):
        self.check_error("((1)", calc_expr.UnclosedParenError, 0)

    def test_call_paren(self):
        self.check_error("max(1,", calc_expr.UnclosedParenError, 3)
        self.check_error("2 * abs(3", calc_expr.UnclosedParenError, 7)

    def test_invalid_character_inside_paren(self):
        self.check_unexpected("( $", 2)


class HierarchyTest(unittest.TestCase):  # interface
    def test_error_classes(self):
        classes = [calc_expr.UnexpectedTokenError, calc_expr.UnclosedParenError, calc_expr.UnknownNameError,
                   calc_expr.ArityError, calc_expr.DivisionByZeroError, calc_expr.DomainError]
        self.assertTrue(issubclass(calc_expr.CalcError, Exception))
        for cls in classes:
            self.assertIn(calc_expr.CalcError, cls.__bases__)
            for other in classes:
                if other is not cls:
                    self.assertFalse(issubclass(cls, other))

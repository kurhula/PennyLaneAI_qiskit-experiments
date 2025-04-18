# This code is part of Qiskit.
#
# (C) Copyright IBM 2017, 2024.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
# pylint: disable=invalid-name

"""Tests for error mitigation routines."""

import unittest
from collections import Counter
from test.base import QiskitExperimentsTestCase

import numpy as np

from qiskit import QiskitError, QuantumCircuit
from qiskit.quantum_info import Statevector
from qiskit.quantum_info.operators.predicates import matrix_equal
from qiskit.result import Counts
from qiskit.result.utils import marginal_counts
from qiskit.providers.fake_provider import GenericBackendV2

from qiskit_experiments.data_processing import (
    CorrelatedReadoutMitigator,
    LocalReadoutMitigator,
)
from qiskit_experiments.data_processing.mitigation import (
    counts_probability_vector,
    expval_with_stddev,
    stddev,
    str2diag,
)


class Property:
    """Class representing a single backend property with name and value"""

    def __init__(self, name, value):
        self.name = name
        self.value = value


class Properties:
    """Class holding backend properties in the qubits attribute"""

    def __init__(self, probs):
        self.probs = probs
        self.qubits = [
            [
                Property("prob_meas1_prep0", q_probs[0]),
                Property("prob_meas0_prep1", q_probs[1]),
            ]
            for q_probs in probs
        ]


class MitigatorTestBackend(GenericBackendV2):
    """Custom backend with a .properties().qubits with readout error values

    This backend's properties are formatted as qiskit-ibm-runtime is as of March 2025
    """

    def __init__(self, num_qubits: int, probs: list[tuple[float, float]]):
        self.probs = probs
        super().__init__(num_qubits=num_qubits)

    def properties(self):
        """Return backend properties"""
        return Properties(self.probs)


class TestReadoutMitigation(QiskitExperimentsTestCase):
    """Tests for correlated and local readout mitigation."""

    def setUp(self):
        super().setUp()
        self.rng = np.random.default_rng(42)

        # Probabilities copied from Qiskit 1's qiskit.providers.fake_provider.Fake5QV1
        probs = [
            [0.049000000000000044, 0.0776],
            [0.021399999999999975, 0.0408],
            [0.09160000000000001, 0.1388],
            [0.013, 0.04239999999999999],
            [0.086, 0.49860000000000004],
        ]

        self.backend = MitigatorTestBackend(num_qubits=5, probs=probs)

    @staticmethod
    def compare_results(res1, res2):
        """Compare the results between two runs"""
        res1_total_shots = sum(res1.values())
        res2_total_shots = sum(res2.values())
        keys = set(res1.keys()).union(set(res2.keys()))
        total = 0
        for key in keys:
            val1 = res1.get(key, 0) / res1_total_shots
            val2 = res2.get(key, 0) / res2_total_shots
            total += abs(val1 - val2) ** 2
        return total

    @staticmethod
    def mitigators(assignment_matrices, qubits=None):
        """Generates the mitigators to test for given assignment matrices"""
        full_assignment_matrix = assignment_matrices[0]
        for m in assignment_matrices[1:]:
            full_assignment_matrix = np.kron(full_assignment_matrix, m)
        CRM = CorrelatedReadoutMitigator(full_assignment_matrix, qubits)
        LRM = LocalReadoutMitigator(assignment_matrices, qubits)
        mitigators = [CRM, LRM]
        return mitigators

    def simulate_circuit(self, circuit, assignment_matrix, num_qubits, shots=1024):
        """Simulates the given circuit under the given readout noise"""
        probs = Statevector.from_instruction(circuit).probabilities()
        noisy_probs = assignment_matrix @ probs
        labels = [bin(a)[2:].zfill(num_qubits) for a in range(2**num_qubits)]
        results = self.rng.choice(labels, size=shots, p=noisy_probs)
        return Counts(dict(Counter(results)))

    @staticmethod
    def ghz_3_circuit():
        """A 3-qubit circuit generating |000>+|111>"""
        c = QuantumCircuit(3)
        c.h(0)
        c.cx(0, 1)
        c.cx(1, 2)
        return (c, "ghz_3_ciruit", 3)

    @staticmethod
    def first_qubit_h_3_circuit():
        """A 3-qubit circuit generating |000>+|001>"""
        c = QuantumCircuit(3)
        c.h(0)
        return (c, "first_qubit_h_3_circuit", 3)

    def assignment_matrices(self):
        """A 3-qubit readout noise assignment matrices"""
        return LocalReadoutMitigator(backend=self.backend)._assignment_mats[0:3]

    def counts_data(self, circuit, assignment_matrices, shots=1024):
        """Generates count data for the noisy and noiseless versions of the circuit simulation"""
        full_assignment_matrix = assignment_matrices[0]
        for m in assignment_matrices[1:]:
            full_assignment_matrix = np.kron(full_assignment_matrix, m)
        num_qubits = len(assignment_matrices)
        ideal_assignment_matrix = np.eye(2**num_qubits)
        counts_ideal = self.simulate_circuit(circuit, ideal_assignment_matrix, num_qubits, shots)
        counts_noise = self.simulate_circuit(circuit, full_assignment_matrix, num_qubits, shots)
        probs_noise = {key: value / shots for key, value in counts_noise.items()}
        return counts_ideal, counts_noise, probs_noise

    def test_mitigation_improvement(self):
        """Test whether readout mitigation led to more accurate results"""
        shots = 1024
        assignment_matrices = self.assignment_matrices()
        mitigators = self.mitigators(assignment_matrices)
        circuit, circuit_name, num_qubits = self.ghz_3_circuit()
        counts_ideal, counts_noise, probs_noise = self.counts_data(
            circuit, assignment_matrices, shots
        )
        unmitigated_error = self.compare_results(counts_ideal, counts_noise)
        unmitigated_stddev = stddev(probs_noise, shots)

        for mitigator in mitigators:
            mitigated_quasi_probs = mitigator.quasi_probabilities(counts_noise)
            mitigated_probs = (
                mitigated_quasi_probs.nearest_probability_distribution().binary_probabilities(
                    num_bits=num_qubits
                )
            )
            mitigated_error = self.compare_results(counts_ideal, mitigated_probs)
            self.assertLess(
                mitigated_error,
                unmitigated_error * 0.8,
                f"Mitigator {mitigator} did not improve circuit {circuit_name} measurements",
            )
            mitigated_stddev_upper_bound = mitigated_quasi_probs._stddev_upper_bound
            max_unmitigated_stddev = max(unmitigated_stddev.values())
            self.assertGreaterEqual(
                mitigated_stddev_upper_bound,
                max_unmitigated_stddev,
                f"Mitigator {mitigator} on circuit {circuit_name} gave stddev upper bound "
                f"{mitigated_stddev_upper_bound} while unmitigated stddev maximum is "
                f"{max_unmitigated_stddev}",
            )

    def test_expectation_improvement(self):
        """Test whether readout mitigation led to more accurate results
        and that its standard deviation is increased"""
        shots = 1024
        assignment_matrices = self.assignment_matrices()
        mitigators = self.mitigators(assignment_matrices)
        num_qubits = len(assignment_matrices)
        diagonals = []
        diagonals.append("IZ0")
        diagonals.append("101")
        diagonals.append("IZZ")
        qubit_index = {i: i for i in range(num_qubits)}
        circuit, circuit_name, num_qubits = self.ghz_3_circuit()
        counts_ideal, counts_noise, _ = self.counts_data(circuit, assignment_matrices, shots)
        probs_ideal, _ = counts_probability_vector(counts_ideal, qubit_index=qubit_index)
        probs_noise, _ = counts_probability_vector(counts_noise, qubit_index=qubit_index)
        for diagonal in diagonals:
            if isinstance(diagonal, str):
                diagonal = str2diag(diagonal)
            unmitigated_expectation, unmitigated_stddev = expval_with_stddev(
                diagonal, probs_noise, shots=counts_noise.shots()
            )
            ideal_expectation = np.dot(probs_ideal, diagonal)
            unmitigated_error = np.abs(ideal_expectation - unmitigated_expectation)
            for mitigator in mitigators:
                mitigated_expectation, mitigated_stddev = mitigator.expectation_value(
                    counts_noise, diagonal
                )
                mitigated_error = np.abs(ideal_expectation - mitigated_expectation)
                self.assertLess(
                    mitigated_error,
                    unmitigated_error,
                    f"Mitigator {mitigator} did not improve circuit {circuit_name} expectation "
                    f"computation for diagonal {diagonal} ideal: {ideal_expectation}, unmitigated:"
                    f" {unmitigated_expectation} mitigated: {mitigated_expectation}",
                )
                self.assertGreaterEqual(
                    mitigated_stddev,
                    unmitigated_stddev,
                    f"Mitigator {mitigator} did not increase circuit {circuit_name} the"
                    f" standard deviation",
                )

    def test_clbits_parameter(self):
        """Test whether the clbits parameter is handled correctly"""
        shots = 10000
        assignment_matrices = self.assignment_matrices()
        mitigators = self.mitigators(assignment_matrices)
        circuit, _, _ = self.first_qubit_h_3_circuit()
        counts_ideal, counts_noise, _ = self.counts_data(circuit, assignment_matrices, shots)
        counts_ideal_12 = marginal_counts(counts_ideal, [1, 2])
        counts_ideal_02 = marginal_counts(counts_ideal, [0, 2])

        for mitigator in mitigators:
            mitigated_probs_12 = (
                mitigator.quasi_probabilities(counts_noise, qubits=[1, 2], clbits=[1, 2])
                .nearest_probability_distribution()
                .binary_probabilities(num_bits=2)
            )
            mitigated_error = self.compare_results(counts_ideal_12, mitigated_probs_12)
            self.assertLess(
                mitigated_error,
                0.001,
                f"Mitigator {mitigator} did not correctly marginalize for qubits 1,2",
            )

            mitigated_probs_02 = (
                mitigator.quasi_probabilities(counts_noise, qubits=[0, 2], clbits=[0, 2])
                .nearest_probability_distribution()
                .binary_probabilities(num_bits=2)
            )
            mitigated_error = self.compare_results(counts_ideal_02, mitigated_probs_02)
            self.assertLess(
                mitigated_error,
                0.001,
                f"Mitigator {mitigator} did not correctly marginalize for qubits 0,2",
            )

    def test_qubits_parameter(self):
        """Test whether the qubits parameter is handled correctly"""
        shots = 10000
        assignment_matrices = self.assignment_matrices()
        mitigators = self.mitigators(assignment_matrices)
        circuit, _, _ = self.first_qubit_h_3_circuit()
        counts_ideal, counts_noise, _ = self.counts_data(circuit, assignment_matrices, shots)
        counts_ideal_012 = counts_ideal
        counts_ideal_210 = Counts({"000": counts_ideal["000"], "100": counts_ideal["001"]})
        counts_ideal_102 = Counts({"000": counts_ideal["000"], "010": counts_ideal["001"]})

        for mitigator in mitigators:
            mitigated_probs_012 = (
                mitigator.quasi_probabilities(counts_noise, qubits=[0, 1, 2])
                .nearest_probability_distribution()
                .binary_probabilities(num_bits=3)
            )
            mitigated_error = self.compare_results(counts_ideal_012, mitigated_probs_012)
            self.assertLess(
                mitigated_error,
                0.001,
                f"Mitigator {mitigator} did not correctly handle qubit order 0, 1, 2",
            )

            mitigated_probs_210 = (
                mitigator.quasi_probabilities(counts_noise, qubits=[2, 1, 0])
                .nearest_probability_distribution()
                .binary_probabilities(num_bits=3)
            )
            mitigated_error = self.compare_results(counts_ideal_210, mitigated_probs_210)
            self.assertLess(
                mitigated_error,
                0.001,
                f"Mitigator {mitigator} did not correctly handle qubit order 2, 1, 0",
            )

            mitigated_probs_102 = (
                mitigator.quasi_probabilities(counts_noise, qubits=[1, 0, 2])
                .nearest_probability_distribution()
                .binary_probabilities(num_bits=3)
            )
            mitigated_error = self.compare_results(counts_ideal_102, mitigated_probs_102)
            self.assertLess(
                mitigated_error,
                0.001,
                "Mitigator {mitigator} did not correctly handle qubit order 1, 0, 2",
            )

    def test_repeated_qubits_parameter(self):
        """Tests the order of mitigated qubits."""
        shots = 10000
        assignment_matrices = self.assignment_matrices()
        mitigators = self.mitigators(assignment_matrices, qubits=[0, 1, 2])
        circuit, _, _ = self.first_qubit_h_3_circuit()
        counts_ideal, counts_noise, _ = self.counts_data(circuit, assignment_matrices, shots)
        counts_ideal_012 = counts_ideal
        counts_ideal_210 = Counts({"000": counts_ideal["000"], "100": counts_ideal["001"]})

        for mitigator in mitigators:
            mitigated_probs_210 = (
                mitigator.quasi_probabilities(counts_noise, qubits=[2, 1, 0])
                .nearest_probability_distribution()
                .binary_probabilities(num_bits=3)
            )
            mitigated_error = self.compare_results(counts_ideal_210, mitigated_probs_210)
            self.assertLess(
                mitigated_error,
                0.001,
                f"Mitigator {mitigator} did not correctly handle qubit order 2,1,0",
            )

            # checking qubit order 2,1,0 should not "overwrite" the default 0,1,2
            mitigated_probs_012 = (
                mitigator.quasi_probabilities(counts_noise)
                .nearest_probability_distribution()
                .binary_probabilities(num_bits=3)
            )
            mitigated_error = self.compare_results(counts_ideal_012, mitigated_probs_012)
            self.assertLess(
                mitigated_error,
                0.001,
                f"Mitigator {mitigator} did not correctly handle qubit order 0,1,2 "
                f"(the expected default)",
            )

    def test_qubits_subset_parameter(self):
        """Tests mitigation on a subset of the initial set of qubits."""

        shots = 10000
        assignment_matrices = self.assignment_matrices()
        mitigators = self.mitigators(assignment_matrices, qubits=[2, 4, 6])
        circuit, _, _ = self.first_qubit_h_3_circuit()
        counts_ideal, counts_noise, _ = self.counts_data(circuit, assignment_matrices, shots)
        counts_ideal_2 = marginal_counts(counts_ideal, [0])
        counts_ideal_6 = marginal_counts(counts_ideal, [2])

        for mitigator in mitigators:
            mitigated_probs_2 = (
                mitigator.quasi_probabilities(counts_noise, qubits=[2])
                .nearest_probability_distribution()
                .binary_probabilities(num_bits=1)
            )
            mitigated_error = self.compare_results(counts_ideal_2, mitigated_probs_2)
            self.assertLess(
                mitigated_error,
                0.001,
                "Mitigator {mitigator} did not correctly handle qubit subset",
            )

            mitigated_probs_6 = (
                mitigator.quasi_probabilities(counts_noise, qubits=[6])
                .nearest_probability_distribution()
                .binary_probabilities(num_bits=1)
            )
            mitigated_error = self.compare_results(counts_ideal_6, mitigated_probs_6)
            self.assertLess(
                mitigated_error,
                0.001,
                f"Mitigator {mitigator} did not correctly handle qubit subset",
            )
            diagonal = str2diag("ZZ")
            ideal_expectation = 0
            mitigated_expectation, _ = mitigator.expectation_value(
                counts_noise, diagonal, qubits=[2, 6]
            )
            mitigated_error = np.abs(ideal_expectation - mitigated_expectation)
            self.assertLess(
                mitigated_error,
                0.1,
                f"Mitigator {mitigator} did not improve circuit expectation",
            )

    def test_from_backend(self):
        """Test whether a local mitigator can be created directly from backend properties"""

        LRM_from_backend = LocalReadoutMitigator(backend=self.backend)

        mats = []
        probs = self.backend.probs
        for qubit_idx in range(self.backend.num_qubits):
            mat = np.array(
                [
                    [1 - probs[qubit_idx][0], probs[qubit_idx][1]],
                    [probs[qubit_idx][0], 1 - probs[qubit_idx][1]],
                ]
            )
            mats.append(mat)
        LRM_from_matrices = LocalReadoutMitigator(assignment_matrices=mats)
        self.assertTrue(
            matrix_equal(
                LRM_from_backend.assignment_matrix(), LRM_from_matrices.assignment_matrix()
            )
        )

    def test_error_handling(self):
        """Test that the assignment matrices are valid."""
        bad_matrix_A = np.array([[-0.3, 1], [1.3, 0]])  # negative indices
        bad_matrix_B = np.array([[0.2, 1], [0.7, 0]])  # columns not summing to 1
        good_matrix_A = np.array([[0.2, 1], [0.8, 0]])
        for bad_matrix in [bad_matrix_A, bad_matrix_B]:
            with self.assertRaises(QiskitError) as cm:
                CorrelatedReadoutMitigator(bad_matrix)
            self.assertEqual(
                cm.exception.message,
                "Assignment matrix columns must be valid probability distributions",
            )

        with self.assertRaises(QiskitError) as cm:
            amats = [good_matrix_A, bad_matrix_A]
            LocalReadoutMitigator(amats)
        self.assertEqual(
            cm.exception.message,
            "Assignment matrix columns must be valid probability distributions",
        )

        with self.assertRaises(QiskitError) as cm:
            amats = [bad_matrix_B, good_matrix_A]
            LocalReadoutMitigator(amats)
        self.assertEqual(
            cm.exception.message,
            "Assignment matrix columns must be valid probability distributions",
        )

    def test_expectation_value_endian(self):
        """Test that endian for expval is little."""
        assignment_matrices = self.assignment_matrices()
        mitigators = self.mitigators(assignment_matrices)
        counts = Counts({"10": 3, "11": 24, "00": 74, "01": 923})
        for mitigator in mitigators:
            expval, _ = mitigator.expectation_value(counts, diagonal="IZ", qubits=[0, 1])
            self.assertAlmostEqual(expval, -1.0, places=0)

    def test_quasi_probabilities_shots_passing(self):
        """Test output of LocalReadoutMitigator.quasi_probabilities

        We require the number of shots to be set in the output.
        """
        mitigator = LocalReadoutMitigator([np.array([[0.9, 0.1], [0.1, 0.9]])], qubits=[0])
        counts = Counts({"10": 3, "11": 24, "00": 74, "01": 923})
        quasi_dist = mitigator.quasi_probabilities(counts)
        self.assertEqual(quasi_dist.shots, sum(counts.values()))

        # custom number of shots
        quasi_dist = mitigator.quasi_probabilities(counts, shots=1025)
        self.assertEqual(quasi_dist.shots, 1025)

    def test_local_readout_assignment_matrix(self):
        """Tests that the local mitigator generates the full assignment matrix correctly"""
        qubits = [7, 2, 3]
        assignment_matrices = LocalReadoutMitigator(backend=self.backend)._assignment_mats[0:3]
        expected_assignment_matrix = np.kron(
            np.kron(assignment_matrices[2], assignment_matrices[1]), assignment_matrices[0]
        )
        expected_mitigation_matrix = np.linalg.inv(expected_assignment_matrix)
        LRM = LocalReadoutMitigator(assignment_matrices, qubits)
        self.assertTrue(matrix_equal(expected_mitigation_matrix, LRM.mitigation_matrix()))
        self.assertTrue(matrix_equal(expected_assignment_matrix, LRM.assignment_matrix()))


if __name__ == "__main__":
    unittest.main()

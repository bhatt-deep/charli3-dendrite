from abc import ABC
from abc import abstractmethod
from typing import Optional

from pycardano import Address
from pycardano import PlutusData

from cardex.dataclasses.models import Assets
from cardex.dexs.base_classes import BasePoolState


class AbstractPoolState(ABC, BasePoolState):
    """A particular pool state, either current or historical."""

    datum_parsed: PlutusData

    @property
    @abstractmethod
    def pool_id(self) -> str:
        """A unique identifier for the pool.

        This is a unique string differentiating this pool from every other pool on the
        dex, and is necessary for dexs that have more than one pool for a pair but with
        different fee structures.
        """
        raise NotImplementedError("Unique pool id is not specified.")

    @property
    @abstractmethod
    def dex(self) -> str:
        raise NotImplementedError("DEX name is undefined.")

    @abstractmethod
    def get_amount_out(self, asset: Assets) -> tuple[Assets, float]:
        raise NotImplementedError("")

    @property
    @abstractmethod
    def pool_datum_class(self) -> type[PlutusData]:
        raise NotImplementedError

    @property
    @abstractmethod
    def order_datum_class(self) -> type[PlutusData]:
        raise NotImplementedError

    @property
    @abstractmethod
    def pool_datum(self) -> PlutusData:
        """The pool state datum."""
        if not self.datum_parsed:
            if not self.datum_cbor:
                raise ValueError("No datum specified.")
            self.datum_parsed = self.pool_datum_class.from_cbor(self.datum_cbor)

        return self.datum_parsed

    @abstractmethod
    def swap_tx_output(
        self,
        address: Address,
        in_assets: Assets,
        out_assets: Assets,
        slippage: float = 0.005,
        forward_address: Optional[Address] = None,
    ):
        raise NotImplementedError("Swap transaction output not specified.")


class AbstractConstantProductPoolState(AbstractPoolState):
    def get_amount_out(self, asset: Assets) -> tuple[Assets, float]:
        """Get the output asset amount given an input asset amount.

        Args:
            asset: An asset with a defined quantity.

        Returns:
            A tuple where the first value is the estimated asset returned from the swap
                and the second value is the price impact ratio.
        """
        assert len(asset) == 1, "Asset should only have one token."
        assert asset.unit() in [
            self.unit_a,
            self.unit_b,
        ], f"Asset {asset.unit} is invalid for pool {self.unit_a}-{self.unit_b}"

        if asset.unit() == self.unit_a:
            reserve_in, reserve_out = self.reserve_a, self.reserve_b
            unit_out = self.unit_b
        else:
            reserve_in, reserve_out = self.reserve_b, self.reserve_a
            unit_out = self.unit_a

        # Calculate the amount out
        fee_modifier = 10000 - self.volume_fee()
        numerator: int = asset.quantity() * fee_modifier * reserve_out
        denominator: int = asset.quantity() * fee_modifier + reserve_in * 10000
        amount_out = Assets(**{unit_out: numerator // denominator})

        if amount_out.quantity() == 0:
            return amount_out, 0

        # Calculate the price impact
        price_numerator: int = (
            reserve_out * asset.quantity() * denominator * fee_modifier
            - numerator * reserve_in * 10000
        )
        price_denominator: int = reserve_out * asset.quantity() * denominator * 10000
        price_impact: float = price_numerator / price_denominator

        return amount_out, price_impact


class AbstractStableSwapPoolState(AbstractPoolState):
    @property
    def amp(cls) -> Assets:
        return 75

    def _get_D(self) -> float:
        """Regression to learn the stability constant."""
        # TODO: Expand this to operate on pools with more than one stable
        N_COINS = 2
        Ann = self.amp * N_COINS**N_COINS
        S = self.reserve_a + self.reserve_b
        if S == 0:
            return 0

        # Iterate until the change in value is <1 unit.
        D = S
        for i in range(256):
            D_P = D**3 / (N_COINS**N_COINS * self.reserve_a * self.reserve_b)
            D_prev = D
            D = D * (Ann * S + D_P * N_COINS) / ((Ann - 1) * D + (N_COINS + 1) * D_P)

            if abs(D - D_prev) < 1:
                break

        return D

    def _get_y(self, in_assets: Assets, out_unit: str):
        """Calculate the output amount using a regression."""
        N_COINS = 2
        Ann = self.amp * N_COINS**N_COINS
        D = self._get_D()

        # Make sure only one input supplied
        if len(in_assets) > 1:
            raise ValueError("Only one input asset allowed.")
        elif in_assets.unit() not in [self.unit_a, self.unit_b]:
            raise ValueError("Invalid input token.")
        elif out_unit not in [self.unit_a, self.unit_b]:
            raise ValueError("Invalid output token.")

        in_quantity = in_assets.quantity() * (10000 - self.volume_fee) / 10000
        if in_assets.unit() == self.unit_a:
            in_reserve = self.reserve_a + in_quantity
        else:
            in_reserve = self.reserve_b + in_quantity

        S = in_reserve
        c = D**3 / (N_COINS**2 * Ann * in_reserve)
        b = S + D / Ann
        out_prev = 0
        out = D

        for i in range(256):
            out_prev = out
            out = (out**2 + c) / (2 * out + b - D)

            if abs(out - out_prev) < 1:
                break

        return Assets(**{out_unit: int(out)})

    def get_amount_out(self, asset: Assets) -> tuple[Assets, float]:
        out_unit = self.unit_a if asset.unit() == self.unit_b else self.unit_b
        out_asset = self._get_y(asset, out_unit)
        out_reserve = self.reserve_b if out_unit == self.unit_b else self.reserve_a
        out_asset.__root__[out_asset.unit()] = int(out_reserve - out_asset.quantity())
        return out_asset, 0
